"""
run.py — one session of the agent.

    python3 -m agent.run                 mock account, live models, no orders
    python3 -m agent.run --live --dry    real chain + account, still no orders
    python3 -m agent.run --live          real, places orders

Order of operations, not negotiable:

    1. snapshot     what does the account look like
    2. candidates   which contracts are even eligible
    3. preflight    may the agent act at all
    4. propose      one model suggests a trade
    5. challenge    a second model attacks it
    6. arbitrate    deterministic code rules
    7. revise       exactly one round, if warranted
    8. challenge    the adversary sees the revision
    9. arbitrate    final ruling
   10. execute      place whatever survived
   11. record       JSON + markdown, every run, including failures

Any exception means: place nothing, write what is known.
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from agent import gate, debate
from agent.broker import MockOptionsBroker, AlpacaOptionsBroker, BrokerError
from agent.options_data import OptionsData, APPROVED_UNDERLYINGS


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- state


def state_path(mode):
    return os.path.join(DATA, f"state_{mode}.json")


def load_state(mode):
    """
    A MISSING state file initialises the system.
    A MALFORMED state file halts it.

    Those are very different situations. Swallowing both meant a corrupted
    safety file silently reset the block streak and daily order count —
    the same class of bug as a filter reading an absent field: it looks
    like it is working.
    """
    default = {
        "block_streak": 0,
        "runs": 0,
        "trades_by_date": {},
        "day_start_equity": {},
        "last_run": None,
    }

    try:
        with open(state_path(mode)) as f:
            d = json.load(f)

        if not isinstance(d, dict):
            raise ValueError("state file does not contain an object")

        default.update(d)

    except FileNotFoundError:
        # Legitimate first run.
        pass

    except (OSError, json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(
            f"State file for {mode!r} is unreadable; refusing to continue. "
            f"Inspect {state_path(mode)} by hand."
        ) from e

    return default


def save_state(mode, s):
    os.makedirs(DATA, exist_ok=True)

    tmp = state_path(mode) + ".tmp"

    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)

    # Atomic replacement.
    os.replace(tmp, state_path(mode))


# --------------------------------------------------------------- records


def write_records(record, mode):
    """One JSON per session, one markdown entry appended per day."""

    d = record["date"]

    os.makedirs(os.path.join(DATA, "decisions"), exist_ok=True)
    os.makedirs(os.path.join(DATA, "journal"), exist_ok=True)

    n = record["session"]

    jpath = os.path.join(
        DATA,
        "decisions",
        f"{d}-{n:02d}-{mode}.json",
    )

    with open(jpath, "w") as f:
        json.dump(record, f, indent=2, default=str)

    mpath = os.path.join(DATA, "journal", f"{d}.md")

    new = not os.path.exists(mpath)

    with open(mpath, "a") as f:
        if new:
            f.write(f"# {d}\n")

        f.write(render_debate(record))

    return jpath, mpath


def render_debate(r) -> str:
    """The artifact a human reads. Proposed, objected, resolved."""

    L = [
        f"\n## Session {r['session']} — "
        f"{r['time']} ({r['mode']})\n"
    ]

    if r.get("blocks"):
        L.append("**The agent did not act.**\n")

        for b in r["blocks"]:
            L.append(f"- {b['detail']}")

        L.append("")
        return "\n".join(L) + "\n"

    L.append(
        f"Equity ${r['equity']:,.2f} · "
        f"cash ${r['cash']:,.2f} "
        f"({r['free_cash']:,.2f} free) · "
        f"{r['eligible_count']} eligible contracts\n"
    )

    p = r.get("proposal")

    if not p or p.get("action") == "no_trade":
        L.append("**Proposed:** nothing.\n")

        if p:
            L.append(f"> {p.get('reasoning', '')}\n")

        return "\n".join(L) + "\n"

    L.append(
        f"**Proposed** — {p['action'].replace('_', ' ')}, "
        f"{p['contracts']}x `{p['contract_symbol']}` "
        f"@ ${p['strike']:,.2f}, expires {p['expiry']}. "
        f"Premium ${p['expected_premium']:,.2f}/contract "
        f"(${p['total_premium']:,.2f} total). "
        f"Max loss ${p['max_loss']:,.2f}.\n"
    )

    L.append(f"> {p['reasoning']}\n")

    if p.get("corrections"):
        L.append("*Arithmetic corrected by the parser:*\n")

        for c in p["corrections"]:
            L.append(f"- {c}")

        L.append("")

    o = r.get("objection")

    if o:
        L.append(
            f"**Challenged** — `{o['failure_mode']}`, "
            f"severity {o['severity']:.2f}.\n"
        )

        L.append(f"> {o['objection']}\n")

        if o.get("what_would_fix_it"):
            L.append(
                f"*Suggested fix:* "
                f"{o['what_would_fix_it']}\n"
            )

    rev = r.get("revision")

    if rev and rev.get("action") != "no_trade":
        L.append(
            f"**Revised** — {rev['contracts']}x "
            f"`{rev['contract_symbol']}` "
            f"@ ${rev['strike']:,.2f}, "
            f"premium "
            f"${rev['expected_premium']:,.2f}/contract.\n"
        )

        L.append(f"> {rev['reasoning']}\n")

        o2 = r.get("objection2")

        if o2:
            L.append(
                f"**Challenged again** — "
                f"`{o2['failure_mode']}`, "
                f"severity {o2['severity']:.2f}.\n"
            )

            L.append(f"> {o2['objection']}\n")

    ru = r.get("ruling")

    if ru:
        L.append(
            f"**Ruled** — {ru['outcome'].upper()}. "
            f"{ru['rationale']}\n"
        )

    if r.get("fills"):
        L.append("**Placed:**\n")

        for f_ in r["fills"]:
            L.append(
                f"- {f_['side']} "
                f"{f_['contracts']}x "
                f"{f_['symbol']} — "
                f"{f_.get('status', 'submitted')}"
            )

        L.append("")

    elif r.get("dry"):
        L.append("*Dry run — nothing was placed.*\n")

    else:
        L.append("*No order placed.*\n")

    return "\n".join(L) + "\n"


# ------------------------------------------------------------------- run


def main(argv=None):
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--live",
        action="store_true",
        help="real account and chain",
    )

    ap.add_argument(
        "--dry",
        action="store_true",
        help="decide but place nothing",
    )

    args = ap.parse_args(argv)

    mode = (
        ("live" if args.live else "mock")
        + ("-dry" if args.dry else "")
    )

    log(f"starting — {mode}")

    # Important: live, live-dry, mock, and mock-dry keep
    # completely separate persistent state.
    state_mode = mode

    st = load_state(state_mode)

    d = today()

    session = (
        st["trades_by_date"]
        .get(d, {})
        .get("sessions", 0)
        + 1
    )

    record = {
        "date": d,
        "session": session,
        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "mode": mode,
        "dry": args.dry,
        "blocks": [],
        "fills": [],
    }

    snap = None

    try:

        # -------------------------------------------------- 1. snapshot

        broker = (
            AlpacaOptionsBroker()
            if args.live
            else MockOptionsBroker()
        )

        snap = broker.snapshot()

        record.update(
            equity=round(snap.equity, 2),
            cash=round(snap.cash, 2),
            free_cash=round(snap.free_cash(), 2),
            market_open=snap.market_open,
            shares=dict(snap.shares),
            open_options=[
                p.to_dict()
                for p in snap.option_positions
            ],
        )

        log(
            f"equity ${snap.equity:,.2f}  "
            f"cash ${snap.cash:,.2f}  "
            f"market "
            f"{'open' if snap.market_open else 'closed'}  "
            f"{len(snap.option_positions)} options open"
        )

        # Day-start equity is the anchor for the daily-loss halt.
        if d not in st["day_start_equity"]:
            st["day_start_equity"][d] = round(
                snap.equity,
                2,
            )

        # ------------------------------------------------ 2. candidates

        candidates = []

        if args.live:

            data = OptionsData()

            for sym in APPROVED_UNDERLYINGS:
                for kind in ("call", "put"):
                    result = data.screen(sym, kind)
                    candidates.extend(result.eligible)

        else:

            from agent.options_data import Candidate

            candidates = [
                Candidate(
                    "XLE260904C00063000",
                    "XLE",
                    "call",
                    63.0,
                    "2026-09-04",
                    8,
                    0.92,
                    1.02,
                    0.97,
                    0.047,
                    97.0,
                    169,
                    0.4,
                    0.0072,
                ),
                Candidate(
                    "XLE260904C00065000",
                    "XLE",
                    "call",
                    65.0,
                    "2026-09-04",
                    8,
                    0.26,
                    0.30,
                    0.28,
                    0.087,
                    28.0,
                    120,
                    0.5,
                    0.039,
                ),
                Candidate(
                    "XLE260904P00062000",
                    "XLE",
                    "put",
                    62.0,
                    "2026-09-04",
                    8,
                    0.77,
                    0.79,
                    0.78,
                    0.013,
                    78.0,
                    3641,
                    0.2,
                    -0.0088,
                ),
                Candidate(
                    "XLP260904C00086500",
                    "XLP",
                    "call",
                    86.5,
                    "2026-09-04",
                    8,
                    0.89,
                    0.95,
                    0.92,
                    0.065,
                    92.0,
                    105,
                    0.6,
                    0.0007,
                ),
            ]

        record["eligible_count"] = len(candidates)

        record["eligible"] = [
            c.symbol
            for c in candidates
        ]

        # Keep the full market-time candidate snapshot.
        # Saturday's analysis needs to know not only which
        # contract changed but why the alternatives differed.
        record["candidates"] = [
            c.to_dict()
            for c in candidates
        ]

        log(f"{len(candidates)} eligible contracts")

        # -------------------------------------------------- 3. preflight

        pre = gate.preflight(
            snap,
            st["day_start_equity"].get(d),
            st["trades_by_date"]
            .get(d, {})
            .get("orders", 0),
            project_root=ROOT,
        )

        if not pre.may_run:

            record["blocks"] = [
                {
                    "rule": b.rule,
                    "detail": b.detail,
                }
                for b in pre.blocks
            ]

            for b in pre.blocks:
                log(f"BLOCKED  {b}")

            raise SystemExit(0)

        if not candidates:

            record["blocks"] = [
                {
                    "rule": "no_candidates",
                    "detail": (
                        "No contract passed today's "
                        "eligibility filters."
                    ),
                }
            ]

            log("nothing eligible — standing down")

            raise SystemExit(0)

        # ---------------------------------------------------- 4. propose

        proposal = debate.propose(
            snap,
            candidates,
        )

        record["proposal"] = proposal.to_dict()

        if not proposal.is_trade:

            log(
                f"proposer: {proposal.action} — "
                f"{proposal.reasoning[:80]}"
            )

            raise SystemExit(0)

        log(
            f"PROPOSE  "
            f"{proposal.contracts}x "
            f"{proposal.contract_symbol} "
            f"({proposal.action})"
        )

        for c in proposal.corrections:
            log(f"  corrected: {c}")

        # -------------------------------------------------- 5. challenge

        objection = debate.challenge(
            snap,
            proposal,
            candidates,
        )

        record["objection"] = objection.to_dict()

        log(
            f"OBJECT   "
            f"{objection.failure_mode} "
            f"sev={objection.severity:.2f}"
        )

        # -------------------------------------------------- 6. arbitrate

        ruling1 = gate.arbitrate(
            proposal,
            objection,
            snap,
            candidates,
            revision_used=False,
            block_streak=st["block_streak"],
        )

        record["ruling1"] = ruling1.to_dict()

        ruling = ruling1

        log(f"RULING   {ruling.outcome}")

        # ---------------------------------------------------- 7. revise

        if ruling.outcome == "revise":

            revised = debate.revise(
                snap,
                proposal,
                objection,
                candidates,
            )

            record["revision"] = revised.to_dict()

            if revised.is_trade:

                log(
                    f"REVISE   "
                    f"{revised.contracts}x "
                    f"{revised.contract_symbol}"
                )

                objection2 = debate.challenge(
                    snap,
                    revised,
                    candidates,
                )

                record["objection2"] = (
                    objection2.to_dict()
                )

                log(
                    f"OBJECT   "
                    f"{objection2.failure_mode} "
                    f"sev={objection2.severity:.2f}"
                )

                ruling2 = gate.arbitrate(
                    revised,
                    objection2,
                    snap,
                    candidates,
                    revision_used=True,
                    block_streak=st["block_streak"],
                )

                record["ruling2"] = ruling2.to_dict()

                ruling = ruling2

            else:

                log(
                    f"revision: "
                    f"{revised.action}"
                )

                ruling = gate.Ruling(
                    "reject",
                    None,
                    (
                        "The revision produced no trade: "
                        f"{revised.reasoning}"
                    ),
                )

            log(f"RULING   {ruling.outcome}")

        # Keep the old key so existing reporting code
        # does not break.
        record["ruling"] = ruling.to_dict()

        # Explicit final decision for analysis.
        record["final_ruling"] = ruling.to_dict()

        # --------------------------------------------------- 8. execute

        if (
            ruling.outcome == "reject"
            or ruling.proposal is None
        ):

            st["block_streak"] += 1

            log(
                f"no trade "
                f"(block streak "
                f"{st['block_streak']})"
            )

        else:

            p = ruling.proposal

            side = (
                "short"
                if p.action in (
                    "covered_call",
                    "cash_secured_put",
                )
                else "long"
            )

            kind = (
                "call"
                if p.action == "covered_call"
                else "put"
            )

            # ------------------------------------------------
            # FINAL EXECUTION GATE
            #
            # Preflight ran before the model calls.
            # Refresh broker state immediately before action.
            #
            # This re-checks:
            #   - kill switch
            #   - market status
            #   - daily loss halt
            #   - daily order count
            #   - cash / collateral
            #   - share coverage
            #   - open positions
            #   - exact proposal hard limits
            # ------------------------------------------------

            fresh = broker.snapshot()

            final = gate.validate_execution(
                proposal=p,
                snapshot=fresh,
                day_start_equity=(
                    st["day_start_equity"]
                    .get(d)
                ),
                orders_today=(
                    st["trades_by_date"]
                    .get(d, {})
                    .get("orders", 0)
                ),
                candidates=candidates,
                project_root=ROOT,
            )

            record["execution_check"] = (
                final.to_dict()
            )

            if not final.may_execute:

                for b in final.blocks:
                    log(f"FINAL GATE  {b}")

                log(
                    "trade no longer valid — "
                    "nothing placed"
                )

                record["blocks"].extend(
                    [
                        {
                            "rule": b.rule,
                            "detail": b.detail,
                        }
                        for b in final.blocks
                    ]
                )

            else:

                log(
                    "FINAL GATE passed — "
                    "fresh broker state validated"
                )

                if args.dry:

                    # Dry state is isolated from live state,
                    # so resetting its streak is safe.
                    st["block_streak"] = 0

                    log(
                        f"DRY      would "
                        f"{side} "
                        f"{p.contracts}x "
                        f"{p.contract_symbol}"
                    )

                else:

                    try:

                        fill = broker.place_option_order(
                            symbol=p.contract_symbol,
                            underlying=p.underlying,
                            kind=kind,
                            side=side,
                            contracts=p.contracts,
                            strike=p.strike,
                            expiry=p.expiry,
                            premium=p.expected_premium,
                        )

                        record["fills"].append(fill)

                        # Only a successfully accepted order
                        # clears the live block streak.
                        st["block_streak"] = 0

                        log(
                            f"FILL     "
                            f"{side} "
                            f"{p.contracts}x "
                            f"{p.contract_symbol}"
                        )

                        day = (
                            st["trades_by_date"]
                            .setdefault(d, {})
                        )

                        day["orders"] = (
                            day.get("orders", 0)
                            + 1
                        )

                    except BrokerError as e:

                        # Do NOT clear block_streak here.
                        # No successful order occurred.
                        log(
                            f"ORDER FAILED  {e}"
                        )

                        record["order_error"] = str(e)

    except SystemExit:
        pass

    except Exception as e:

        log(
            f"ERROR  "
            f"{type(e).__name__}: "
            f"{e}"
        )

        traceback.print_exc()

        record["blocks"].append(
            {
                "rule": "run_error",
                "detail": (
                    "Run failed with "
                    f"{type(e).__name__}. "
                    "Nothing was placed."
                ),
            }
        )

    finally:

        day = (
            st["trades_by_date"]
            .setdefault(d, {})
        )

        day["sessions"] = session

        st["runs"] = (
            st.get("runs", 0)
            + 1
        )

        st["last_run"] = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
        )

        save_state(
            state_mode,
            st,
        )

        jpath, mpath = write_records(
            record,
            mode,
        )

        log(
            f"wrote "
            f"{os.path.relpath(jpath, ROOT)}"
        )

        log(
            f"wrote "
            f"{os.path.relpath(mpath, ROOT)}"
        )

        log("done")

    return 0


if __name__ == "__main__":
    sys.exit(main())