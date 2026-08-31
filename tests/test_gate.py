"""
test_gate.py — prove the fence holds.

Every test feeds the gate something that SHOULD be refused and checks it was
refused for the right reason. Plus the arbiter's severity bands, the
deadlock floor, and the final execution gate.

    python3 -m tests.test_gate
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import gate
from agent.broker import MockOptionsBroker, OptionPosition
from agent.debate import Proposal, Objection, compute_max_loss


passed = failed = 0


def chk(label, cond, detail=""):
    global passed, failed

    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


class Cand:
    def __init__(
        self,
        symbol,
        underlying,
        kind,
        strike,
        expiry,
        premium,
        oi=500,
        spread=0.04,
    ):
        self.symbol = symbol
        self.underlying = underlying
        self.kind = kind
        self.strike = strike
        self.expiry = expiry
        self.premium = premium
        self.open_interest = oi
        self.spread_pct = spread
        self.days_to_expiry = 8


EXP = "2026-09-04"

CANDS = [
    Cand("XLE260904C00063000", "XLE", "call", 63.0, EXP, 86.0),
    Cand("XLE260904C00063500", "XLE", "call", 63.5, EXP, 62.0),
    Cand("XLE260904C00064000", "XLE", "call", 64.0, EXP, 44.0),
    Cand("XLE260904C00065000", "XLE", "call", 65.0, EXP, 28.0),
    Cand("XLE260904P00062000", "XLE", "put", 62.0, EXP, 78.0),
    Cand("XLE260904P00060000", "XLE", "put", 60.0, EXP, 26.0),
    Cand("XLF260904C00058500", "XLF", "call", 58.5, EXP, 46.0),
    Cand("XLP260904C00086500", "XLP", "call", 86.5, EXP, 92.0),
]

BY = {c.symbol: c for c in CANDS}


def prop(symbol, action, contracts, snap):
    c = BY[symbol]
    spot = snap.spots.get(c.underlying, 0.0)

    return Proposal(
        action=action,
        underlying=c.underlying,
        contract_symbol=symbol,
        strike=c.strike,
        expiry=c.expiry,
        contracts=contracts,
        expected_premium=c.premium,
        total_premium=round(c.premium * contracts, 2),
        max_loss=compute_max_loss(
            action,
            c.strike,
            spot,
            contracts,
            c.premium,
        ),
        reasoning="test",
    )


def fresh():
    return MockOptionsBroker().snapshot()


def rule(res):
    return res.vetoes[0].rule if res.vetoes else None


# ======================================================================
# HARD LIMITS
# ======================================================================

print("\n" + "=" * 70)
print("HARD LIMITS")
print("=" * 70)

snap = fresh()


# --- a legitimate trade gets through ------------------------------------

r = gate.screen(
    prop(
        "XLE260904C00064000",
        "covered_call",
        2,
        snap,
    ),
    snap,
    CANDS,
)

chk(
    "allows a legitimate covered call",
    r.approved,
    r.reasons(),
)


# --- naked calls --------------------------------------------------------

p = prop(
    "XLE260904C00064000",
    "covered_call",
    3,
    snap,
)

p.underlying = "XLE"

s2 = MockOptionsBroker(
    shares={
        "XLE": 100,
        "XLF": 400,
        "XLP": 300,
    }
).snapshot()

p.max_loss = compute_max_loss(
    "covered_call",
    64.0,
    s2.spots["XLE"],
    3,
    44.0,
)

r = gate.screen(
    p,
    s2,
    CANDS,
)

chk(
    "refuses calls beyond the shares held",
    rule(r) in ("uncovered_call", "underlying_cap"),
    rule(r),
)


# --- calls on top of existing shorts ------------------------------------

s3 = MockOptionsBroker().snapshot()

s3.option_positions.append(
    OptionPosition(
        symbol="XLE260904C00063000",
        underlying="XLE",
        kind="call",
        side="short",
        contracts=3,
        strike=63.0,
        expiry=EXP,
        entry_premium=86.0,
        opened="",
    )
)

r = gate.screen(
    prop(
        "XLE260904C00064000",
        "covered_call",
        3,
        s3,
    ),
    s3,
    CANDS,
)

chk(
    "counts existing short calls toward coverage",
    rule(r) in ("uncovered_call", "underlying_cap"),
    rule(r),
)


# --- unapproved underlying ----------------------------------------------

p = prop(
    "XLE260904C00064000",
    "covered_call",
    1,
    snap,
)

p.underlying = "TSLA"

r = gate.screen(
    p,
    snap,
    CANDS,
)

chk(
    "refuses an unapproved underlying",
    rule(r) == "universe",
    rule(r),
)


# --- contract not on the eligible list ----------------------------------

p = prop(
    "XLE260904C00064000",
    "covered_call",
    1,
    snap,
)

p.contract_symbol = "XLE260904C00099000"

r = gate.screen(
    p,
    snap,
    CANDS,
)

chk(
    "refuses a contract that failed eligibility",
    rule(r) == "not_eligible",
    rule(r),
)


# --- too many contracts -------------------------------------------------

r = gate.screen(
    prop(
        "XLE260904C00064000",
        "covered_call",
        9,
        snap,
    ),
    snap,
    CANDS,
)

chk(
    "refuses more than the per-trade cap",
    rule(r) == "max_contracts",
    rule(r),
)


# --- cash-secured put without the cash ----------------------------------

poor = MockOptionsBroker(
    cash=1_000.0
).snapshot()

p = prop(
    "XLE260904P00062000",
    "cash_secured_put",
    1,
    poor,
)

r = gate.screen(
    p,
    poor,
    CANDS,
)

chk(
    "refuses a cash-secured put without collateral",
    rule(r) == "insufficient_collateral",
    rule(r),
)


# --- collateral already pledged -----------------------------------------

s4 = MockOptionsBroker(
    cash=13_000.0
).snapshot()

s4.option_positions.append(
    OptionPosition(
        symbol="XLE260904P00062000",
        underlying="XLE",
        kind="put",
        side="short",
        contracts=2,
        strike=62.0,
        expiry=EXP,
        entry_premium=78.0,
        opened="",
    )
)

p = prop(
    "XLE260904P00060000",
    "cash_secured_put",
    1,
    s4,
)

r = gate.screen(
    p,
    s4,
    CANDS,
)

chk(
    "counts pledged collateral as unavailable",
    rule(r) in (
        "insufficient_collateral",
        "underlying_cap",
    ),
    rule(r),
)


# --- max loss ceiling ----------------------------------------------------

small = MockOptionsBroker(
    cash=60_000.0
).snapshot()

small.equity = 20_000.0

p = prop(
    "XLE260904P00062000",
    "cash_secured_put",
    1,
    small,
)

r = gate.screen(
    p,
    small,
    CANDS,
)

chk(
    "refuses a position whose max loss is too large",
    "max_loss" in [v.rule for v in r.vetoes],
    [v.rule for v in r.vetoes],
)


# ======================================================================
# PREFLIGHT
# ======================================================================

print("\n" + "=" * 70)
print("PREFLIGHT")
print("=" * 70)

with tempfile.TemporaryDirectory() as tmp:

    snap = fresh()

    r = gate.preflight(
        snap,
        100_000,
        0,
        project_root=tmp,
    )

    chk(
        "runs normally when all is well",
        r.may_run,
        [b.rule for b in r.blocks],
    )

    # Kill switch.
    open(
        os.path.join(
            tmp,
            gate.HALT_FILE,
        ),
        "w",
    ).close()

    r = gate.preflight(
        snap,
        100_000,
        0,
        project_root=tmp,
    )

    chk(
        "halts on the kill switch",
        not r.may_run
        and any(
            b.rule == "kill_switch"
            for b in r.blocks
        ),
    )

    os.remove(
        os.path.join(
            tmp,
            gate.HALT_FILE,
        )
    )

    # Market closed.
    closed = MockOptionsBroker(
        market_open=False
    ).snapshot()

    r = gate.preflight(
        closed,
        100_000,
        0,
        project_root=tmp,
    )

    chk(
        "stands down when the market is closed",
        not r.may_run
        and any(
            b.rule == "market_closed"
            for b in r.blocks
        ),
    )

    # Daily loss halt.
    r = gate.preflight(
        snap,
        110_000,
        0,
        project_root=tmp,
    )

    chk(
        "halts after a 3%+ daily loss",
        not r.may_run
        and any(
            b.rule == "daily_loss_halt"
            for b in r.blocks
        ),
    )

    # Daily trade cap.
    r = gate.preflight(
        snap,
        100_000,
        4,
        project_root=tmp,
    )

    chk(
        "halts at the daily trade cap",
        not r.may_run
        and any(
            b.rule == "daily_trade_cap"
            for b in r.blocks
        ),
    )


# ======================================================================
# THE ARBITER
# ======================================================================

print("\n" + "=" * 70)
print("THE ARBITER")
print("=" * 70)

snap = fresh()

good = prop(
    "XLE260904C00064000",
    "covered_call",
    2,
    snap,
)


# --- low severity executes ----------------------------------------------

r = gate.arbitrate(
    good,
    Objection(
        0.2,
        "none",
        "minor",
    ),
    snap,
    CANDS,
)

chk(
    "low severity executes",
    r.outcome == "execute",
    r.outcome,
)


# --- low severity must not bypass hard limits ---------------------------

oversized = prop(
    "XLE260904C00064000",
    "covered_call",
    4,
    snap,
)

r = gate.arbitrate(
    oversized,
    Objection(
        0.10,
        "none",
        "minor",
    ),
    snap,
    CANDS,
)

unsafe_execute = (
    r.outcome == "execute"
    and r.proposal is not None
    and r.proposal.contracts > 3
)

chk(
    "low severity cannot bypass the per-trade contract cap",
    not unsafe_execute,
    (
        f"outcome={r.outcome}, "
        f"contracts="
        f"{r.proposal.contracts if r.proposal else None}"
    ),
)


# --- middle severity requests revision ----------------------------------

r = gate.arbitrate(
    good,
    Objection(
        0.65,
        "assignment_risk",
        "close to money",
    ),
    snap,
    CANDS,
    revision_used=False,
)

chk(
    "middle severity sends back for revision",
    r.outcome == "revise",
    r.outcome,
)


# --- middle severity after revision substitutes -------------------------

r = gate.arbitrate(
    good,
    Objection(
        0.65,
        "assignment_risk",
        "still close",
    ),
    snap,
    CANDS,
    revision_used=True,
)

chk(
    "middle severity after revision substitutes something safer",
    r.outcome == "substitute"
    and r.substituted,
    r.outcome,
)

if r.outcome == "substitute":
    print(f"        -> {r.rationale}")


# --- high severity rejects ----------------------------------------------

r = gate.arbitrate(
    good,
    Objection(
        0.9,
        "concentration",
        "way too much",
    ),
    snap,
    CANDS,
)

chk(
    "high severity rejects",
    r.outcome == "reject",
    r.outcome,
)


# --- current deadlock behaviour -----------------------------------------

# --- repeated blocks must never force a trade ----------------------------

r = gate.arbitrate(
    good,
    Objection(
        0.9,
        "concentration",
        "way too much",
    ),
    snap,
    CANDS,
    block_streak=2,
)

chk(
    "three consecutive blocks still resolve to no trade",
    r.outcome == "reject"
    and r.proposal is None,
    f"outcome={r.outcome}",
)

if r.outcome == "reject":
    print(f"        -> {r.rationale[:100]}...")


# --- even an extreme streak must never force execution -------------------

r = gate.arbitrate(
    good,
    Objection(
        0.9,
        "concentration",
        "way too much",
    ),
    snap,
    CANDS,
    block_streak=99,
)

chk(
    "even a long block streak never forces execution",
    r.outcome == "reject"
    and r.proposal is None,
    f"outcome={r.outcome}",
)

# ======================================================================
# SAFER ALTERNATIVES
# ======================================================================

print("\n" + "=" * 70)
print("SAFER ALTERNATIVES ARE CODE-GENERATED")
print("=" * 70)

snap = fresh()

big = prop(
    "XLE260904C00063000",
    "covered_call",
    3,
    snap,
)

alt, how = gate.safer_alternative(
    big,
    snap,
    CANDS,
)

chk(
    "an alternative is found",
    alt is not None,
    how,
)

if alt:

    chk(
        "the alternative is smaller or further out",
        alt.contracts < big.contracts
        or alt.strike > big.strike,
        f"{alt.contracts}x @ {alt.strike}",
    )

    chk(
        "the alternative stays on the eligible list",
        alt.contract_symbol in BY,
        alt.contract_symbol,
    )

    chk(
        "the alternative itself passes the gate",
        gate.screen(
            alt,
            snap,
            CANDS,
        ).approved,
    )

    chk(
        "the alternative never increases size",
        alt.contracts <= big.contracts,
        alt.contracts,
    )

    print(f"        -> {how}")


# ======================================================================
# FINAL EXECUTION GATE
# ======================================================================

print("\n" + "=" * 70)
print("FINAL EXECUTION GATE")
print("=" * 70)


# --- a valid surviving trade still passes on fresh state ----------------

with tempfile.TemporaryDirectory() as tmp:

    fresh_snap = fresh()

    valid_trade = prop(
        "XLE260904C00064000",
        "covered_call",
        2,
        fresh_snap,
    )

    r = gate.validate_execution(
        proposal=valid_trade,
        snapshot=fresh_snap,
        day_start_equity=100_000,
        orders_today=0,
        candidates=CANDS,
        project_root=tmp,
    )

    chk(
        "fresh final gate allows a valid surviving trade",
        r.may_execute,
        [b.rule for b in r.blocks],
    )


# --- kill switch appearing after debate still stops execution ------------

with tempfile.TemporaryDirectory() as tmp:

    fresh_snap = fresh()

    valid_trade = prop(
        "XLE260904C00064000",
        "covered_call",
        2,
        fresh_snap,
    )

    # Imagine initial preflight already passed and the models
    # spent 20–30 seconds deliberating. A human creates HALT
    # before the order reaches the broker.
    open(
        os.path.join(
            tmp,
            gate.HALT_FILE,
        ),
        "w",
    ).close()

    r = gate.validate_execution(
        proposal=valid_trade,
        snapshot=fresh_snap,
        day_start_equity=100_000,
        orders_today=0,
        candidates=CANDS,
        project_root=tmp,
    )

    chk(
        "fresh final gate catches a late kill switch",
        not r.may_execute
        and any(
            b.rule == "kill_switch"
            for b in r.blocks
        ),
        [b.rule for b in r.blocks],
    )


# ======================================================================
# RESULT
# ======================================================================

print("\n" + "=" * 70)
print(f"  {passed} passed, {failed} failed")
print("=" * 70 + "\n")

sys.exit(1 if failed else 0)