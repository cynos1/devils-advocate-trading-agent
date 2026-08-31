"""
debate.py — the two voices.

    PROPOSER   suggests a specific options trade from an approved menu
    ADVERSARY  attacks it, naming which failure mode it found

Neither can execute anything. The proposer picks from a list of contracts
that already passed every eligibility filter, so it cannot invent a contract.
The adversary returns a severity that is clamped into [0, 1] on the way in.

Everything malformed resolves to no trade. The models never get the benefit
of the doubt.

Numbers the model restates — premium, max loss — are verified against the
candidate data rather than trusted. A model that gets arithmetic wrong should
not be able to propagate that error into a risk calculation.
"""

import json
import os
import re
from dataclasses import dataclass, asdict

MODEL = "claude-sonnet-5"

STRATEGIES = ("covered_call", "cash_secured_put", "protective_put", "no_trade")

FAILURE_MODES = (
    "overconfidence",
    "recency_bias",
    "insufficient_premium",
    "poor_liquidity",
    "concentration",
    "assignment_risk",
    "unfavourable_expiry",
    "position_conflict",
    "none",
)

# how far the model's stated premium may differ from the real one
PREMIUM_TOLERANCE = 0.02      # 2%


# ----------------------------------------------------------------- types

@dataclass
class Proposal:
    action: str
    underlying: str = ""
    contract_symbol: str = ""
    strike: float = 0.0
    expiry: str = ""
    contracts: int = 0
    expected_premium: float = 0.0     # PER CONTRACT
    total_premium: float = 0.0        # computed here, never from the model
    max_loss: float = 0.0             # computed here, never from the model
    reasoning: str = ""
    raw: str = ""
    failed: bool = False
    corrections: list = None          # arithmetic the model got wrong

    def __post_init__(self):
        if self.corrections is None:
            self.corrections = []

    def to_dict(self):
        return asdict(self)

    @property
    def is_trade(self):
        return self.action != "no_trade" and not self.failed


@dataclass
class Objection:
    severity: float
    failure_mode: str
    objection: str
    what_would_fix_it: str = ""
    raw: str = ""
    failed: bool = False

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------- prompts

PROPOSER_SYSTEM = """You are the proposing half of an automated options \
agent trading a PAPER account. You suggest one trade per session, or none.

You may only choose from three strategies:

  covered_call      Sell a call against shares already owned. Collects \
premium. Caps upside above the strike. Requires 100 owned shares per contract.

  cash_secured_put  Sell a put with cash set aside to buy the shares if \
assigned. Collects premium. Obligates purchase at the strike. Requires \
strike x 100 in free cash per contract.

  protective_put    Buy a put against shares owned. Costs premium. Floors \
downside below the strike. Requires 100 owned shares per contract. This one \
spends money rather than earning it, so justify it as a hedge or do not \
propose it.

  no_trade          Propose nothing today. A legitimate answer.

CRITICAL CONSTRAINTS

You must select a contract from the eligible list you are given. You cannot \
invent a contract symbol. Every contract on that list has already passed \
liquidity, spread, expiry and open-interest filters.

You are NOT predicting market direction. Do not propose a trade because you \
think the price will rise or fall. Propose based on premium relative to risk, \
what the portfolio already holds, and whether the obligation is acceptable.

UNITS — READ CAREFULLY

expected_premium is PER CONTRACT, in dollars. It must match exactly the \
premium shown for that contract in the eligible list. Do NOT multiply it by \
the number of contracts. If the list says a contract pays $86, then \
expected_premium is 86 whether you propose one contract or four.

Proposing more contracts is not automatically better. More contracts means \
more obligation, more concentration in one underlying, and less room to act \
later in the week. Propose the number the situation justifies.

Respond with ONLY a JSON object. No markdown fences, no preamble.

{"action": "covered_call" | "cash_secured_put" | "protective_put" | "no_trade",
 "underlying": "XLE",
 "contract_symbol": "exact symbol from the eligible list",
 "strike": 63.0,
 "expiry": "2026-09-04",
 "contracts": 1,
 "expected_premium": 86.0,
 "reasoning": "two sentences, first person, plain English"}

For no_trade, set the other fields to empty strings or zeros and explain in \
reasoning.

Keep reasoning under 60 words. A truncated response is discarded and treated \
as a decision to do nothing."""


ADVERSARY_SYSTEM = """You are the adversary in an automated options agent. \
A proposer has suggested a trade. Your only job is to find the strongest \
argument AGAINST it.

You are not a balanced reviewer. You do not weigh both sides. You do not \
have a stake in the trade going through. Find the flaw.

Name exactly one failure mode from this list:

  overconfidence        The reasoning claims more certainty than the \
evidence supports.
  recency_bias          The conclusion rests on the last few sessions of \
price action.
  insufficient_premium  The income does not compensate for the risk assumed \
or the upside surrendered.
  poor_liquidity        Thin open interest, stale last trade, or a spread \
wide enough to erode the premium.
  concentration         Too much of the account tied to one underlying or \
one expiry date.
  assignment_risk       The strike sits too near the money for the time \
remaining.
  unfavourable_expiry   The expiry is too near, too far, or spans a known \
event.
  position_conflict     Contradicts or duplicates something already open.
  none                  You genuinely cannot find a meaningful flaw.

SEVERITY is how strongly you object, from 0 to 1.
  0.0-0.3  a quibble; the trade is basically sound
  0.4-0.6  a real concern that should change the trade
  0.7-1.0  this trade should not happen as proposed

Be honest with severity. If you object strongly to everything, the agent \
never trades and you have failed at your job. Reserve high severity for \
genuine problems.

All figures you are shown have been verified against live market data. \
Premium is stated per contract, and totals are computed by code, not by the \
proposer. Do not object on the grounds that a number looks wrong or stale — \
that check has already been done. Object to the JUDGEMENT, not the \
arithmetic.

Every fact you need is stated explicitly, including the exact number of \
calendar days to expiry. Do NOT infer, estimate, or reason about any figure \
you have not been given — in particular, do not calculate time to expiry \
from the date yourself. If your objection depends on a number, that number \
must appear above. An objection built on a fact you invented is worse than \
no objection at all.

WHAT_WOULD_FIX_IT must be concrete and actionable: a further strike, a later \
expiry, fewer contracts, a different underlying, or nothing if the trade is \
unsalvageable. It must not contradict your own objection — do not object to \
an expiry being too distant and then recommend a later one.

Respond with ONLY a JSON object. No markdown fences, no preamble.

{"severity": 0.5,
 "failure_mode": "assignment_risk",
 "objection": "two sentences naming the specific problem",
 "what_would_fix_it": "one concrete change"}

Keep both text fields under 50 words each. A truncated response is discarded \
and treated as a decision to do nothing."""


# ------------------------------------------------------------- prompt bodies

def build_proposer_prompt(snapshot, candidates, recent_objections=None) -> str:
    lines = [
        f"Account equity: ${snapshot.equity:,.2f}",
        f"Cash: ${snapshot.cash:,.2f} "
        f"(${snapshot.free_cash():,.2f} free after put collateral)",
        "",
        "Shares held:",
    ]
    for sym, n in sorted(snapshot.shares.items()):
        spot = snapshot.spots.get(sym, 0)
        lines.append(f"  {sym}: {n} shares @ ${spot:,.2f} "
                     f"= {n // 100} contracts coverable, "
                     f"{snapshot.open_contracts_on(sym)} already open")

    if snapshot.option_positions:
        lines += ["", "Options already open:"]
        for p in snapshot.option_positions:
            lines.append(f"  {p.side} {p.contracts}x {p.symbol} "
                         f"(strike {p.strike}, expires {p.expiry})")
    else:
        lines += ["", "Options already open: none"]

    lines += ["",
              "Eligible contracts — you must pick from this list.",
              "Premium shown is PER CONTRACT:"]
    for c in candidates:
        lines.append(
            f"  {c.symbol}  {c.kind}  strike ${c.strike:.2f}  "
            f"expires {c.expiry} ({c.days_to_expiry}d)  "
            f"premium ${c.premium:,.0f}/contract  "
            f"spread {c.spread_pct:.1%}  OI {c.open_interest:,}")

    if recent_objections:
        lines += ["", "Objections raised in recent sessions:"]
        for o in recent_objections[-3:]:
            lines.append(f"  {o}")

    lines += ["", "Propose one trade, or no_trade."]
    return "\n".join(lines)


def build_adversary_prompt(snapshot, proposal, candidates) -> str:
    by_symbol = {c.symbol: c for c in (candidates or [])}
    cand = by_symbol.get(proposal.contract_symbol)
    dte = getattr(cand, "days_to_expiry", None)
    spread = getattr(cand, "spread_pct", None)
    oi = getattr(cand, "open_interest", None)

    spot = snapshot.spots.get(proposal.underlying, 0.0)
    moneyness = ((proposal.strike - spot) / spot) if spot else 0.0

    lines = [
        "PROPOSED TRADE",
        f"  strategy      : {proposal.action}",
        f"  contract      : {proposal.contract_symbol}",
        f"  underlying    : {proposal.underlying}, spot ${spot:,.2f}",
        f"  strike        : ${proposal.strike:,.2f} "
        f"({moneyness:+.1%} vs spot)",
        f"  expiry        : {proposal.expiry}"
        + (f"  —  {dte} CALENDAR DAYS FROM TODAY" if dte is not None else ""),
        f"  contracts     : {proposal.contracts}",
        f"  premium       : ${proposal.expected_premium:,.2f} per contract",
        f"  total premium : ${proposal.total_premium:,.2f} (computed)",
        f"  max loss      : ${proposal.max_loss:,.2f} (computed)",
    ]
    if spread is not None:
        lines.append(f"  bid-ask spread: {spread:.1%} of mid")
    if oi is not None:
        lines.append(f"  open interest : {oi:,}")

    lines += [
        "",
        f"  proposer's reasoning: {proposal.reasoning}",
        "",
        "PORTFOLIO CONTEXT",
        f"  equity ${snapshot.equity:,.2f}, cash ${snapshot.cash:,.2f} "
        f"(${snapshot.free_cash():,.2f} free)",
    ]
    for sym, n in sorted(snapshot.shares.items()):
        s = snapshot.spots.get(sym, 0)
        lines.append(f"  {sym}: {n} shares @ ${s:,.2f}, "
                     f"{snapshot.open_contracts_on(sym)} contracts already open")

    same = [c for c in (candidates or [])
            if c.underlying == proposal.underlying
            and c.symbol != proposal.contract_symbol]
    if same:
        lines += ["", "OTHER ELIGIBLE CONTRACTS ON THE SAME UNDERLYING",
                  "(premium per contract; days = calendar days from today)"]
        for c in same[:8]:
            lines.append(
                f"  {c.symbol}  strike ${c.strike:.2f}  "
                f"{c.days_to_expiry}d  premium ${c.premium:,.0f}  "
                f"spread {c.spread_pct:.1%}  OI {c.open_interest:,}")

    lines += ["", "Find the strongest argument against this trade."]
    return "\n".join(lines)


# ------------------------------------------------------- computed figures

def compute_max_loss(action, strike, spot, contracts, premium_per) -> float:
    """
    Never taken from the model. Computed from the trade's structure.

    covered_call      opportunity cost if called away: (spot - strike) x 100
                      per contract, zero if the strike is above spot
    cash_secured_put  (strike x 100 - premium) per contract
    protective_put    premium paid, plus the drop from spot to strike
    """
    n = max(contracts, 0)
    if action == "covered_call":
        return round(max(0.0, (spot - strike)) * 100 * n, 2)
    if action == "cash_secured_put":
        return round((strike * 100 - premium_per) * n, 2)
    if action == "protective_put":
        return round((premium_per + max(0.0, (spot - strike)) * 100) * n, 2)
    return 0.0


# -------------------------------------------------------------- airlocks

def _extract_json(text: str):
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(),
                     flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_proposal(text: str, candidates=None, snapshot=None) -> Proposal:
    """
    candidates: the eligible list. A proposal naming anything else is
    rejected — this is what stops the model inventing a contract, and it is
    also the source of truth for premium and strike.
    """
    by_symbol = {c.symbol: c for c in (candidates or [])}

    data = _extract_json(text)
    if data is None:
        return Proposal("no_trade", reasoning="The proposer returned no "
                        "readable JSON, so nothing was proposed.",
                        raw=text or "", failed=True)

    action = str(data.get("action", "")).strip().lower()
    if action not in STRATEGIES:
        return Proposal("no_trade", reasoning=f"The proposer named an "
                        f"unrecognised strategy ({action!r}).",
                        raw=text or "", failed=True)

    reasoning = str(data.get("reasoning", "")).strip()[:600] or "No reasoning given."

    if action == "no_trade":
        return Proposal("no_trade", reasoning=reasoning, raw=text or "")

    symbol = str(data.get("contract_symbol", "")).strip().upper()
    if by_symbol and symbol not in by_symbol:
        return Proposal("no_trade",
                        reasoning=f"The proposer named a contract "
                                  f"({symbol or 'blank'}) that is not on the "
                                  f"eligible list.",
                        raw=text or "", failed=True)

    def num(field, default=0.0):
        try:
            return float(data.get(field, default))
        except (TypeError, ValueError):
            return default

    contracts = int(num("contracts", 0))
    if contracts < 1:
        return Proposal("no_trade",
                        reasoning="The proposer asked for a non-positive "
                                  "number of contracts.",
                        raw=text or "", failed=True)

    corrections = []
    cand = by_symbol.get(symbol)

    # --- premium: trust the market data, not the model --------------
    stated_premium = num("expected_premium")
    if cand is not None:
        real_premium = cand.premium
        if stated_premium > 0:
            drift = abs(stated_premium - real_premium) / max(real_premium, 1e-9)
            if drift > PREMIUM_TOLERANCE:
                # the classic error: stating the total instead of per contract
                if abs(stated_premium - real_premium * contracts) < 1.0:
                    corrections.append(
                        f"stated premium ${stated_premium:,.2f} was the total "
                        f"for {contracts} contracts, not the per-contract "
                        f"figure; corrected to ${real_premium:,.2f}")
                else:
                    corrections.append(
                        f"stated premium ${stated_premium:,.2f} did not match "
                        f"the market premium ${real_premium:,.2f}; corrected")
        premium = real_premium
        strike = cand.strike
        expiry = cand.expiry
        underlying = cand.underlying
        if abs(num("strike") - strike) > 0.001:
            corrections.append(
                f"stated strike ${num('strike'):,.2f} did not match the "
                f"contract's ${strike:,.2f}; corrected")
    else:
        premium = stated_premium
        strike = num("strike")
        expiry = str(data.get("expiry", "")).strip()
        underlying = str(data.get("underlying", "")).strip().upper()

    # --- max loss: always computed ----------------------------------
    spot = (snapshot.spots.get(underlying, 0.0) if snapshot else 0.0)
    max_loss = compute_max_loss(action, strike, spot, contracts, premium)

    return Proposal(
        action=action,
        underlying=underlying,
        contract_symbol=symbol,
        strike=strike,
        expiry=expiry,
        contracts=contracts,
        expected_premium=round(premium, 2),
        total_premium=round(premium * contracts, 2),
        max_loss=max_loss,
        reasoning=reasoning,
        raw=text or "",
        corrections=corrections,
    )


def parse_objection(text: str) -> Objection:
    data = _extract_json(text)
    if data is None:
        return Objection(1.0, "none",
                         "The adversary returned no readable JSON. Treated as "
                         "a blocking objection.", raw=text or "", failed=True)

    try:
        sev = float(data.get("severity", 1.0))
    except (TypeError, ValueError):
        sev = 1.0
    if sev != sev or sev in (float("inf"), float("-inf")):
        sev = 1.0
    sev = max(0.0, min(1.0, sev))          # the clamp

    mode = str(data.get("failure_mode", "")).strip().lower()
    if mode not in FAILURE_MODES:
        mode = "none"

    return Objection(
        severity=sev,
        failure_mode=mode,
        objection=str(data.get("objection", "")).strip()[:500]
                  or "No objection text given.",
        what_would_fix_it=str(data.get("what_would_fix_it", "")).strip()[:400],
        raw=text or "",
    )


# ------------------------------------------------------------------ calls

def _call(system: str, user: str, client=None) -> str:
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY missing")
        from anthropic import Anthropic
        client = Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=1000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text")


def propose(snapshot, candidates, recent_objections=None, client=None) -> Proposal:
    if not candidates:
        return Proposal("no_trade",
                        reasoning="No contract passed today's eligibility "
                                  "filters, so there was nothing to propose.")
    try:
        text = _call(PROPOSER_SYSTEM,
                     build_proposer_prompt(snapshot, candidates,
                                           recent_objections), client)
        return parse_proposal(text, candidates=candidates, snapshot=snapshot)
    except Exception as e:
        return Proposal("no_trade",
                        reasoning=f"The proposer could not be reached "
                                  f"({type(e).__name__}).", failed=True)


def revise(snapshot, proposal, objection, candidates, client=None) -> Proposal:
    """The one permitted revision. Same proposer, told what the objection was."""
    body = build_proposer_prompt(snapshot, candidates)
    body += (
        "\n\nYOUR PREVIOUS PROPOSAL WAS CHALLENGED\n"
        f"  you proposed : {proposal.contracts}x {proposal.contract_symbol}\n"
        f"  failure mode : {objection.failure_mode}\n"
        f"  objection    : {objection.objection}\n"
        f"  suggested fix: {objection.what_would_fix_it}\n\n"
        "Revise the trade to address this, or return no_trade if the "
        "objection cannot be answered. This is your only revision."
    )
    try:
        text = _call(PROPOSER_SYSTEM, body, client)
        return parse_proposal(text, candidates=candidates, snapshot=snapshot)
    except Exception as e:
        return Proposal("no_trade",
                        reasoning=f"The revision could not be reached "
                                  f"({type(e).__name__}).", failed=True)


def challenge(snapshot, proposal, candidates, client=None) -> Objection:
    try:
        text = _call(ADVERSARY_SYSTEM,
                     build_adversary_prompt(snapshot, proposal, candidates),
                     client)
        return parse_objection(text)
    except Exception as e:
        return Objection(1.0, "none",
                         f"The adversary could not be reached "
                         f"({type(e).__name__}). Treated as blocking.",
                         failed=True)
