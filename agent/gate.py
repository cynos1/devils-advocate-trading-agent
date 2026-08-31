"""
gate.py — the deterministic risk engine.

Two models argue. This decides. It is plain Python, and that is the point:
two models arguing with a third deciding is a system with no floor. Two
models arguing with deterministic arbitration is a system whose behaviour
you can state exactly.

Three responsibilities:

  preflight(...)   may the agent act at all today?
  screen(...)      does this specific trade satisfy every hard limit?
  arbitrate(...)   given the adversary's severity, what happens?

Nothing here calls a model. Nothing here calls the network. Every limit is
a Python conditional, not a sentence in a prompt — a model can be argued out
of an instruction, it cannot be argued out of a function that refuses to
return.
"""

import os
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- limits

APPROVED_UNDERLYINGS = ("XLE", "XLF", "XLP")

MAX_CONTRACTS_PER_TRADE = 3
MAX_OPTIONS_NOTIONAL_PCT = 0.40      # of equity
MAX_LOSS_PER_POSITION_PCT = 0.05     # of equity
MAX_TRADES_PER_DAY = 4
DAILY_LOSS_HALT_PCT = 0.03           # halt if down this much on the day
HALT_FILE = "HALT"

# severity bands — PROVISIONAL, calibrate Saturday against the observed
# distribution. See BUILD_LOG open items.
# Calibrated 28 Aug 2026 against 30 controlled benchmark cases:
# 24 injected defects + 6 clean controls.
# At these thresholds:
# - 23/24 flawed cases trigger revision or rejection
# - 5/6 clean controls proceed
# - 0/6 clean controls are hard-blocked
SEVERITY_PROCEED = 0.60
SEVERITY_BLOCK = 0.70

DEADLOCK_STREAK = 3                  # consecutive blocks before persistent-deadlock flag


# ----------------------------------------------------------------- types

@dataclass
class Veto:
    rule: str
    detail: str

    def __repr__(self):
        return f"[{self.rule}] {self.detail}"


@dataclass
class PreflightResult:
    may_run: bool
    blocks: list = field(default_factory=list)

    def to_dict(self):
        return {"may_run": self.may_run,
                "blocks": [asdict(b) for b in self.blocks]}


@dataclass
class ScreenResult:
    approved: bool
    vetoes: list = field(default_factory=list)
    checks_passed: list = field(default_factory=list)

    def to_dict(self):
        return {"approved": self.approved,
                "vetoes": [asdict(v) for v in self.vetoes],
                "checks_passed": list(self.checks_passed)}

    def reasons(self):
        return [v.detail for v in self.vetoes]


@dataclass
class Ruling:
    """The arbiter's final word."""
    outcome: str            # "execute" | "revise" | "reject" | "substitute"
    proposal: object = None       # what will actually be placed, if anything
    rationale: str = ""
    substituted: bool = False
    original_contracts: int = 0

    def to_dict(self):
        return {"outcome": self.outcome, "rationale": self.rationale,
                "substituted": self.substituted,
                "original_contracts": self.original_contracts,
                "proposal": self.proposal.to_dict() if self.proposal else None}


# -------------------------------------------------------------- preflight

def preflight(snapshot, day_start_equity, trades_today,
              project_root=".") -> PreflightResult:
    """May the agent act at all today?"""
    blocks = []

    halt = os.path.join(project_root, HALT_FILE)
    if os.path.exists(halt):
        blocks.append(Veto("kill_switch",
                           f"{HALT_FILE} file present — human halt in effect."))

    if not snapshot.market_open:
        blocks.append(Veto("market_closed",
                           "Market is closed. No action taken."))

    if day_start_equity and day_start_equity > 0:
        change = (snapshot.equity - day_start_equity) / day_start_equity
        if change < -DAILY_LOSS_HALT_PCT:
            blocks.append(Veto("daily_loss_halt",
                               f"Down {abs(change):.2%} on the day, past the "
                               f"{DAILY_LOSS_HALT_PCT:.0%} limit. Halting."))

    if trades_today >= MAX_TRADES_PER_DAY:
        blocks.append(Veto("daily_trade_cap",
                           f"{trades_today} trades already placed today; "
                           f"cap is {MAX_TRADES_PER_DAY}."))

    return PreflightResult(may_run=not blocks, blocks=blocks)


# ---------------------------------------------------------------- screen

def screen(proposal, snapshot, candidates=None) -> ScreenResult:
    """Every hard limit, checked against one proposal."""
    vetoes, passed = [], []
    u = proposal.underlying
    n = proposal.contracts

    # 1. universe
    if u not in APPROVED_UNDERLYINGS:
        vetoes.append(Veto("universe",
                           f"{u} is not in the approved universe "
                           f"{list(APPROVED_UNDERLYINGS)}."))
    else:
        passed.append("universe")

    # 2. contract came from the eligible list
    if candidates is not None:
        symbols = {c.symbol for c in candidates}
        if proposal.contract_symbol not in symbols:
            vetoes.append(Veto("not_eligible",
                               f"{proposal.contract_symbol} did not pass "
                               f"today's eligibility filters."))
        else:
            passed.append("eligible_contract")

    # 3. size
    if n < 1:
        vetoes.append(Veto("bad_size", f"{n} contracts is not a valid size."))
    elif n > MAX_CONTRACTS_PER_TRADE:
        vetoes.append(Veto("max_contracts",
                           f"{n} contracts exceeds the "
                           f"{MAX_CONTRACTS_PER_TRADE} per-trade limit."))
    else:
        passed.append("contract_count")

    lots = snapshot.lots_held(u)
    already = snapshot.open_contracts_on(u)

    # 4. covering — the hardest rule in the system
    if proposal.action == "covered_call":
        short_calls = snapshot.short_calls_on(u)
        if short_calls + n > lots:
            vetoes.append(Veto("uncovered_call",
                               f"{n} more short calls on {u} would make "
                               f"{short_calls + n} against {lots} lots held. "
                               f"Naked calls are not permitted."))
        else:
            passed.append("call_covered")

    elif proposal.action == "protective_put":
        long_puts = snapshot.long_puts_on(u)
        if long_puts + n > lots:
            vetoes.append(Veto("overhedged",
                               f"{n} more protective puts on {u} would make "
                               f"{long_puts + n} against {lots} lots held."))
        else:
            passed.append("put_covered")

        cost = proposal.expected_premium * n
        if cost > snapshot.free_cash():
            vetoes.append(Veto("insufficient_cash",
                               f"Buying ${cost:,.2f} of premium with "
                               f"${snapshot.free_cash():,.2f} free cash."))
        else:
            passed.append("cash_available")

    # 5. collateral for short puts
    elif proposal.action == "cash_secured_put":
        needed = proposal.strike * 100 * n
        free = snapshot.free_cash()
        if needed > free:
            vetoes.append(Veto("insufficient_collateral",
                               f"A cash-secured put needs ${needed:,.2f} set "
                               f"aside; only ${free:,.2f} is free."))
        else:
            passed.append("collateral_reserved")

    # 6. per-underlying concentration, derived from shares held
    if already + n > max(lots, 1):
        vetoes.append(Veto("underlying_cap",
                           f"{already + n} open contracts on {u} would exceed "
                           f"the {lots} lots held."))
    else:
        passed.append("underlying_cap")

    # 7. total options exposure
    notional = sum(p.strike * 100 * p.contracts
                   for p in snapshot.option_positions)
    notional += proposal.strike * 100 * n
    ceiling = snapshot.equity * MAX_OPTIONS_NOTIONAL_PCT
    if notional > ceiling:
        vetoes.append(Veto("notional_cap",
                           f"Total options notional ${notional:,.2f} would "
                           f"exceed the ${ceiling:,.2f} ceiling "
                           f"({MAX_OPTIONS_NOTIONAL_PCT:.0%} of equity)."))
    else:
        passed.append("notional_cap")

    # 8. max loss on this position
    loss_ceiling = snapshot.equity * MAX_LOSS_PER_POSITION_PCT
    if proposal.max_loss > loss_ceiling:
        vetoes.append(Veto("max_loss",
                           f"Max loss ${proposal.max_loss:,.2f} exceeds the "
                           f"${loss_ceiling:,.2f} limit "
                           f"({MAX_LOSS_PER_POSITION_PCT:.0%} of equity)."))
    else:
        passed.append("max_loss")

    return ScreenResult(approved=not vetoes, vetoes=vetoes, checks_passed=passed)


# ------------------------------------------------------- safer alternatives

def safer_alternative(proposal, snapshot, candidates):
    """
    Generated by CODE, never by a model. Two moves only:

      1. fewer contracts — halve, then one
      2. a further-out strike on the same underlying and expiry

    Returns a new proposal that passes screen(), or None.
    """
    import copy

    # 1. fewer contracts
    for n in (proposal.contracts // 2, 1):
        if n < 1 or n >= proposal.contracts:
            continue
        alt = copy.deepcopy(proposal)
        alt.contracts = n
        alt.total_premium = round(alt.expected_premium * n, 2)
        from agent.debate import compute_max_loss
        alt.max_loss = compute_max_loss(
            alt.action, alt.strike,
            snapshot.spots.get(alt.underlying, 0.0), n, alt.expected_premium)
        if screen(alt, snapshot, candidates).approved:
            return alt, f"reduced from {proposal.contracts} to {n} contracts"

    # 2. step the strike further out of the money
    if candidates:
        spot = snapshot.spots.get(proposal.underlying, 0.0)
        same = [c for c in candidates
                if c.underlying == proposal.underlying
                and c.expiry == proposal.expiry
                and c.symbol != proposal.contract_symbol]

        if proposal.action in ("covered_call",):
            further = sorted((c for c in same
                              if c.kind == "call" and c.strike > proposal.strike),
                             key=lambda c: c.strike)
        else:
            further = sorted((c for c in same
                              if c.kind == "put" and c.strike < proposal.strike),
                             key=lambda c: -c.strike)

        from agent.debate import compute_max_loss
        for c in further:
            alt = copy.deepcopy(proposal)
            alt.contract_symbol = c.symbol
            alt.strike = c.strike
            alt.expected_premium = c.premium
            alt.total_premium = round(c.premium * alt.contracts, 2)
            alt.max_loss = compute_max_loss(
                alt.action, c.strike, spot, alt.contracts, c.premium)
            if screen(alt, snapshot, candidates).approved:
                return alt, (f"strike moved from ${proposal.strike:.2f} to "
                             f"${c.strike:.2f}")

    return None, ""


@dataclass
class ExecutionCheck:
    may_execute: bool
    blocks: list = field(default_factory=list)

    def to_dict(self):
        return {"may_execute": self.may_execute,
                "blocks": [asdict(b) for b in self.blocks]}


def validate_execution(proposal, snapshot, day_start_equity, orders_today,
                       candidates=None, project_root=".") -> ExecutionCheck:
    """
    The last thing before an order goes out.

    Preflight runs before two model calls that take ~30 seconds. In that
    window the market can close, a human can create the HALT file, equity
    can move through the loss halt, or another process can place an order.
    This revalidates the exact surviving trade against a FRESH snapshot.

    It does not re-screen the whole universe — only this one trade.
    """
    blocks = []

    pre = preflight(snapshot, day_start_equity, orders_today,
                    project_root=project_root)
    blocks.extend(pre.blocks)

    scr = screen(proposal, snapshot, candidates)
    blocks.extend(scr.vetoes)

    return ExecutionCheck(may_execute=not blocks, blocks=blocks)


# -------------------------------------------------------------- arbitrate

def arbitrate(proposal, objection, snapshot, candidates,
              revision_used=False, block_streak=0) -> Ruling:
    """
    Given a proposal and the adversary's objection, decide what happens.

      severity < 0.40   execute as proposed
      0.40 - 0.70       one revision; if already used, try a safer alternative
      severity > 0.70   reject; repeated blocks never force a trade

    The severity thresholds are the only tunable numbers here, and they are
    provisional until calibrated.
    """
    sev = objection.severity

    # ------------------------------------------------------------------
    # ORDER MATTERS.
    #
    # The debate resolves FIRST. The gate then applies to whatever the
    # debate settled on.
    #
    # An earlier version screened before reading severity, which meant any
    # proposal tripping a hard limit skipped the argument entirely — the
    # adversary's objection was computed, displayed, and ignored. That
    # undercuts the premise of the system.
    #
    # The gate is still authoritative: nothing executes without passing
    # screen(). But a fixable size violation now goes back for revision
    # like any other objection, and the revision may fix both problems at
    # once.
    # ------------------------------------------------------------------

    def gated(p, why_prefix):
        """Apply the gate to a settled proposal. Substitute or reject."""
        scr = screen(p, snapshot, candidates)
        if scr.approved:
            return Ruling("execute", p, why_prefix)
        alt, how = safer_alternative(p, snapshot, candidates)
        if alt is not None:
            return Ruling("substitute", alt,
                          f"{why_prefix} The trade then failed a hard limit "
                          f"({scr.vetoes[0].rule}), so the gate substituted a "
                          f"safer version: {how}.",
                          substituted=True,
                          original_contracts=p.contracts)
        return Ruling("reject", None,
                      f"{why_prefix} The trade then failed a hard limit and no "
                      f"safer alternative was available. {scr.vetoes[0].detail}")

    # --- the debate settles it ---------------------------------------

    if sev < SEVERITY_PROCEED:
        return gated(proposal,
                     f"The adversary raised {objection.failure_mode} at "
                     f"severity {sev:.2f}, below the {SEVERITY_PROCEED:.2f} "
                     f"threshold.")

    if sev <= SEVERITY_BLOCK:
        if not revision_used:
            # a hard-limit failure is worth telling the proposer about, so
            # the one revision can address both at once
            scr = screen(proposal, snapshot, candidates)
            extra = ""
            if not scr.approved:
                extra = (f" It also failed a hard limit: "
                         f"{scr.vetoes[0].detail}")
            return Ruling("revise", proposal,
                          f"The adversary raised {objection.failure_mode} at "
                          f"severity {sev:.2f}. Sending back for one "
                          f"revision.{extra}")
        # The revision was the chance to answer the objection. If it still
        # stands at middle severity, the trade should shrink rather than go
        # through unchanged — otherwise the second objection has no effect
        # at all and the revision round is theatre.
        alt, how = safer_alternative(proposal, snapshot, candidates)
        if alt is not None and screen(alt, snapshot, candidates).approved:
            return Ruling("substitute", alt,
                          f"The revision still drew {objection.failure_mode} "
                          f"at severity {sev:.2f}. Substituted a safer "
                          f"version: {how}.",
                          substituted=True,
                          original_contracts=proposal.contracts)
        return gated(proposal,
                     f"The revision still drew {objection.failure_mode} at "
                     f"severity {sev:.2f}, and no smaller alternative "
                     f"existed.")

    # blocking severity
    if block_streak >= DEADLOCK_STREAK - 1:
        return Ruling(
            "reject",
            None,
            f"Persistent deadlock: the adversary has blocked "
            f"{block_streak + 1} consecutive sessions. "
            f"No trade is forced; standing down for this session. "
            f"Flagged for human review."
        )

    return Ruling("reject", None,
                  f"The adversary raised {objection.failure_mode} at severity "
                  f"{sev:.2f}, above the {SEVERITY_BLOCK:.2f} blocking "
                  f"threshold. No trade today.")
