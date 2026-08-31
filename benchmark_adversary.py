"""
benchmark_adversary.py — 30-case calibration benchmark for Devil's Advocate.

Purpose:
    Measure how the adversary scores controlled proposals before tuning
    SEVERITY_PROCEED / SEVERITY_BLOCK.

This script NEVER places an order. It does not instantiate AlpacaOptionsBroker
and does not call any broker order method. It only calls debate.challenge()
against synthetic snapshots/candidate sets.

Run:
    python3 benchmark_adversary.py --limit 3
    python3 benchmark_adversary.py
    python3 benchmark_adversary.py --case poor_liquidity_2

Outputs:
    data/calibration/adversary_<timestamp>.json
    data/calibration/adversary_<timestamp>.csv
"""

import argparse
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
from agent import debate
from agent.broker import MockOptionsBroker, OptionPosition
from agent.debate import Proposal, compute_max_loss


ROOT = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(ROOT, "data", "calibration")


@dataclass
class BenchCandidate:
    symbol: str
    underlying: str
    kind: str
    strike: float
    expiry: str
    days_to_expiry: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    premium: float
    open_interest: int
    last_trade_age: float
    moneyness: float

    # Compatibility aliases in case debate.py uses a slightly different name.
    @property
    def last_trade_age_days(self):
        return self.last_trade_age

    @property
    def bid_ask_spread_pct(self):
        return self.spread_pct

    def to_dict(self):
        return asdict(self)


def occ_symbol(underlying, kind, strike, expiry="2026-09-04"):
    """Build a plausible OCC-style symbol for benchmark readability."""
    yy = expiry[2:4]
    mm = expiry[5:7]
    dd = expiry[8:10]
    cp = "C" if kind == "call" else "P"
    strike_code = f"{int(round(strike * 1000)):08d}"
    return f"{underlying}{yy}{mm}{dd}{cp}{strike_code}"


def candidate(
    underlying,
    kind,
    strike,
    premium,
    *,
    expiry="2026-09-04",
    dte=7,
    oi=500,
    spread=0.05,
    last_age=0.3,
    spot=None,
):
    if spot is None:
        spot = {"XLE": 62.55, "XLF": 58.20, "XLP": 86.44}[underlying]

    mid = max(premium / 100.0, 0.01)
    half_gap = max(mid * spread / 2.0, 0.005)
    bid = max(mid - half_gap, 0.01)
    ask = mid + half_gap
    moneyness = (strike - spot) / spot if spot else 0.0

    return BenchCandidate(
        symbol=occ_symbol(underlying, kind, strike, expiry),
        underlying=underlying,
        kind=kind,
        strike=round(strike, 2),
        expiry=expiry,
        days_to_expiry=dte,
        bid=round(bid, 4),
        ask=round(ask, 4),
        mid=round(mid, 4),
        spread_pct=spread,
        premium=float(premium),
        open_interest=int(oi),
        last_trade_age=float(last_age),
        moneyness=round(moneyness, 6),
    )


def proposal_from_candidate(c, action, contracts, snap, reasoning):
    spot = snap.spots.get(c.underlying, 0.0)
    return Proposal(
        action=action,
        underlying=c.underlying,
        contract_symbol=c.symbol,
        strike=c.strike,
        expiry=c.expiry,
        contracts=contracts,
        expected_premium=c.premium,
        total_premium=round(c.premium * contracts, 2),
        max_loss=compute_max_loss(
            action, c.strike, spot, contracts, c.premium
        ),
        reasoning=reasoning,
    )


def base_snapshot():
    return MockOptionsBroker().snapshot()


def calls_universe(snap, underlying="XLE", dte=7):
    spot = snap.spots[underlying]
    strikes = [
        round(spot + 0.45, 2),
        round(spot + 0.95, 2),
        round(spot + 1.45, 2),
        round(spot + 2.45, 2),
    ]
    premiums = [90, 70, 50, 32]
    return [
        candidate(
            underlying, "call", strike, premium,
            dte=dte, oi=400 + i * 140, spread=0.04 + i * 0.01,
            last_age=0.2 + i * 0.1, spot=spot,
        )
        for i, (strike, premium) in enumerate(zip(strikes, premiums))
    ]


def puts_universe(snap, underlying="XLE", dte=7):
    spot = snap.spots[underlying]
    strikes = [
        round(spot - 0.55, 2),
        round(spot - 1.05, 2),
        round(spot - 1.55, 2),
        round(spot - 2.55, 2),
    ]
    premiums = [84, 64, 46, 30]
    return [
        candidate(
            underlying, "put", strike, premium,
            dte=dte, oi=500 + i * 160, spread=0.04 + i * 0.01,
            last_age=0.2 + i * 0.1, spot=spot,
        )
        for i, (strike, premium) in enumerate(zip(strikes, premiums))
    ]


def case(case_id, expected_mode, snap, candidates, proposal, note, clean=False):
    return {
        "case_id": case_id,
        "expected_mode": expected_mode,
        "clean": clean,
        "note": note,
        "snapshot": snap,
        "candidates": candidates,
        "proposal": proposal,
    }


def build_cases():
    cases = []

    # ------------------------------------------------------------------
    # 1) OVERCONFIDENCE — numbers are healthy; only the reasoning
    # contains unjustified certainty.
    # ------------------------------------------------------------------
    for i, reasoning in enumerate([
        "This trade is guaranteed to generate a good outcome because the setup cannot meaningfully go wrong.",
        "The market conditions make this an essentially certain winner, so there is no need to consider alternative outcomes.",
        "I am completely confident this position will perform as intended; the evidence leaves no meaningful uncertainty.",
    ], start=1):
        snap = base_snapshot()
        spot = snap.spots["XLE"]

        safe = candidate(
            "XLE",
            "call",
            round(spot + 3.00, 2),
            65,
            dte=7,
            oi=1200,
            spread=0.03,
            last_age=0.1,
            spot=spot,
        )

        cands = [
            safe,
            candidate(
                "XLE",
                "call",
                round(spot + 3.50, 2),
                55,
                dte=7,
                oi=1000,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            ),
        ]

        p = proposal_from_candidate(
            safe,
            "covered_call",
            1,
            snap,
            reasoning,
        )

        cases.append(case(
            f"overconfidence_{i}",
            "overconfidence",
            snap,
            cands,
            p,
            "Injected defect: unsupported certainty in the model reasoning.",
        ))

    # ------------------------------------------------------------------
    # 2) RECENCY BIAS — decision is explicitly anchored on a very recent
    # move rather than the full state.
    # ------------------------------------------------------------------
    for i, reasoning in enumerate([
        "Premium jumped yesterday, so I expect the same strength to continue and am prioritizing this contract.",
        "The last session was strong for XLE options; that recent move is the main reason to sell this call now.",
        "Because today's quote is richer than the last quote I saw, the recent premium trend should persist into expiry.",
    ], start=1):
        snap = base_snapshot()
        spot = snap.spots["XLE"]

        safe = candidate(
            "XLE",
            "call",
            round(spot + 3.00, 2),
            65,
            dte=7,
            oi=1200,
            spread=0.03,
            last_age=0.1,
            spot=spot,
        )

        cands = [
            safe,
            candidate(
                "XLE",
                "call",
                round(spot + 3.50, 2),
                55,
                dte=7,
                oi=1000,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            ),
        ]

        p = proposal_from_candidate(
            safe,
            "covered_call",
            1,
            snap,
            reasoning,
        )

        cases.append(case(
            f"recency_bias_{i}",
            "recency_bias",
            snap,
            cands,
            p,
            "Injected defect: recent observations are treated as predictive.",
        ))

    # ------------------------------------------------------------------
    # 3) INSUFFICIENT PREMIUM — proposed contract pays materially less
    # than comparable nearby alternatives.
    # ------------------------------------------------------------------
    for i, premium in enumerate([12, 16, 20], start=1):
        snap = base_snapshot()
        cands = calls_universe(snap)
        bad = deepcopy(cands[2])
        bad.premium = float(premium)
        bad.mid = premium / 100.0
        bad.bid = max(bad.mid - 0.01, 0.01)
        bad.ask = bad.mid + 0.01
        bad.symbol = occ_symbol("XLE", "call", bad.strike, bad.expiry)
        cands[2] = bad
        p = proposal_from_candidate(
            bad, "covered_call", 2, snap,
            "Choosing this contract for income while keeping the strike modestly out of the money."
        )
        cases.append(case(
            f"insufficient_premium_{i}", "insufficient_premium", snap, cands, p,
            f"Injected defect: only ${premium}/contract despite stronger nearby alternatives."
        ))

    # ------------------------------------------------------------------
    # 4) POOR LIQUIDITY — low OI / wide spread while alternatives are liquid.
    # ------------------------------------------------------------------
    liquidity_settings = [(12, 0.40), (22, 0.30), (35, 0.22)]
    for i, (oi, spread) in enumerate(liquidity_settings, start=1):
        snap = base_snapshot()
        cands = calls_universe(snap)
        bad = deepcopy(cands[1])
        bad.open_interest = oi
        bad.spread_pct = spread
        bad.bid_ask_spread_pct  # alias sanity
        half_gap = max(bad.mid * spread / 2.0, 0.01)
        bad.bid = max(bad.mid - half_gap, 0.01)
        bad.ask = bad.mid + half_gap
        cands[1] = bad
        p = proposal_from_candidate(
            bad, "covered_call", 2, snap,
            "The premium is acceptable, so I am selecting this contract."
        )
        cases.append(case(
            f"poor_liquidity_{i}", "poor_liquidity", snap, cands, p,
            f"Injected defect: OI={oi}, spread={spread:.0%} while alternatives are liquid."
        ))

    # ------------------------------------------------------------------
    # 5) CONCENTRATION — existing XLE exposure plus another same-name trade.
    # ------------------------------------------------------------------
    existing_counts = [1, 2, 3]
    for i, existing in enumerate(existing_counts, start=1):
        snap = base_snapshot()
        spot = snap.spots["XLE"]
        snap.option_positions.append(OptionPosition(
            symbol=occ_symbol("XLE", "call", round(spot + 2.5, 2)),
            underlying="XLE", kind="call", side="short",
            contracts=existing, strike=round(spot + 2.5, 2),
            expiry="2026-09-04", entry_premium=35.0, opened=""
        ))
        cands = calls_universe(snap)
        p = proposal_from_candidate(
            cands[1], "covered_call", 2, snap,
            "Adding another XLE income position because the individual contract looks attractive."
        )
        cases.append(case(
            f"concentration_{i}", "concentration", snap, cands, p,
            f"Injected defect: {existing} existing short XLE contracts before adding more."
        ))

    # ------------------------------------------------------------------
    # 6) ASSIGNMENT RISK — near/inside-the-money short calls.
    # ------------------------------------------------------------------
    for i, offset in enumerate([-0.35, 0.00, 0.20], start=1):
        snap = base_snapshot()
        spot = snap.spots["XLE"]
        risky = candidate(
            "XLE", "call", round(spot + offset, 2), 95,
            dte=7, oi=900, spread=0.04, last_age=0.1, spot=spot
        )
        alternatives = calls_universe(snap)
        cands = [risky] + alternatives
        p = proposal_from_candidate(
            risky, "covered_call", 2, snap,
            "Selling the near-money call to maximize premium income."
        )
        cases.append(case(
            f"assignment_risk_{i}", "assignment_risk", snap, cands, p,
            f"Injected defect: strike is {offset:+.2f} versus spot with 7 DTE."
        ))

    # ------------------------------------------------------------------
    # 7) UNFAVOURABLE EXPIRY — very near expiry when a similar 7-DTE choice exists.
    # ------------------------------------------------------------------
    for i, dte in enumerate([1, 2, 3], start=1):
        snap = base_snapshot()
        spot = snap.spots["XLE"]
        expiry = {1: "2026-08-29", 2: "2026-08-30", 3: "2026-08-31"}[dte]
        bad = candidate(
            "XLE", "call", round(spot + 1.5, 2), 52,
            expiry=expiry, dte=dte, oi=600, spread=0.05,
            last_age=0.2, spot=spot
        )
        cands = [bad] + calls_universe(snap, dte=7)
        p = proposal_from_candidate(
            bad, "covered_call", 2, snap,
            "Choosing the shortest expiry even though a similar 7-day contract is available."
        )
        cases.append(case(
            f"unfavourable_expiry_{i}", "unfavourable_expiry", snap, cands, p,
            f"Injected defect: {dte} DTE while comparable 7-DTE alternatives exist."
        ))

    # ------------------------------------------------------------------
    # 8) POSITION CONFLICT — proposal duplicates an already-open short put.
    # ------------------------------------------------------------------
    for i, contracts_open in enumerate([1, 2, 3], start=1):
        snap = base_snapshot()
        cands = puts_universe(snap)
        chosen = cands[1]
        snap.option_positions.append(OptionPosition(
            symbol=chosen.symbol, underlying="XLE", kind="put", side="short",
            contracts=contracts_open, strike=chosen.strike, expiry=chosen.expiry,
            entry_premium=chosen.premium, opened=""
        ))
        p = proposal_from_candidate(
            chosen, "cash_secured_put", 1, snap,
            "Opening this cash-secured put without changing the existing options position."
        )
        cases.append(case(
            f"position_conflict_{i}", "position_conflict", snap, cands, p,
            f"Injected defect: same short-put position is already open ({contracts_open} contracts)."
        ))

    # ------------------------------------------------------------------
    # 9) SIX CLEAN CONTROLS — deliberately healthy proposals.
    #
    # These are meant to be true negative controls: good liquidity,
    # reasonable premium, comfortable moneyness, normal expiry, small
    # size, no conflicting open position, and cautious reasoning.
    # ------------------------------------------------------------------
    clean_specs = [
        ("XLE", "covered_call", 1, "call", 2.50, 72),
        ("XLE", "covered_call", 2, "call", 3.00, 66),
        ("XLF", "covered_call", 1, "call", 2.50, 68),
        ("XLP", "covered_call", 1, "call", 3.00, 70),
        ("XLE", "cash_secured_put", 1, "put", -2.50, 70),
        ("XLF", "cash_secured_put", 1, "put", -2.50, 66),
    ]

    for i, (underlying, action, contracts, kind, strike_offset, premium) in enumerate(
        clean_specs, start=1
    ):
        snap = base_snapshot()
        spot = snap.spots[underlying]

        chosen = candidate(
            underlying,
            kind,
            round(spot + strike_offset, 2),
            premium,
            dte=7,
            oi=1500,
            spread=0.025,
            last_age=0.1,
            spot=spot,
        )

        # Give the adversary nearby alternatives that are also healthy,
        # so it cannot reasonably object that the selected contract is
        # obviously dominated on premium or liquidity.
        if kind == "call":
            alt1 = candidate(
                underlying,
                "call",
                round(spot + strike_offset + 0.50, 2),
                max(premium - 6, 25),
                dte=7,
                oi=1300,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            )
            alt2 = candidate(
                underlying,
                "call",
                round(spot + strike_offset + 1.00, 2),
                max(premium - 12, 25),
                dte=7,
                oi=1200,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            )
        else:
            alt1 = candidate(
                underlying,
                "put",
                round(spot + strike_offset - 0.50, 2),
                max(premium - 6, 25),
                dte=7,
                oi=1300,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            )
            alt2 = candidate(
                underlying,
                "put",
                round(spot + strike_offset - 1.00, 2),
                max(premium - 12, 25),
                dte=7,
                oi=1200,
                spread=0.03,
                last_age=0.1,
                spot=spot,
            )

        cands = [chosen, alt1, alt2]

        reasoning = (
            "This is a modest, inventory-aware position selected from a liquid "
            "eligible set. Premium is reasonable relative to nearby contracts, "
            "the strike is comfortably out of the money, and I am not assuming "
            "a directional forecast."
        )

        p = proposal_from_candidate(
            chosen,
            action,
            contracts,
            snap,
            reasoning,
        )

        cases.append(case(
            f"clean_{i}",
            "clean",
            snap,
            cands,
            p,
            "Clean control: healthy liquidity, reasonable premium, normal expiry, "
            "comfortable moneyness, modest size, and no deliberate defect.",
            clean=True,
        ))

    assert len(cases) == 30, len(cases)
    return cases


def quantile(values, q):
    """Linear quantile, standard-library only."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def serialize_snapshot(snap):
    return {
        "equity": snap.equity,
        "cash": snap.cash,
        "market_open": snap.market_open,
        "shares": dict(snap.shares),
        "spots": dict(snap.spots),
        "open_options": [p.to_dict() for p in snap.option_positions],
    }


def run_case(c):
    objection = debate.challenge(
        c["snapshot"],
        c["proposal"],
        c["candidates"],
    )

    result = {
        "case_id": c["case_id"],
        "expected_mode": c["expected_mode"],
        "clean": c["clean"],
        "note": c["note"],
        "proposal": c["proposal"].to_dict(),
        "candidates": [x.to_dict() for x in c["candidates"]],
        "snapshot": serialize_snapshot(c["snapshot"]),
        "objection": objection.to_dict(),
        "observed_mode": objection.failure_mode,
        "severity": float(objection.severity),
    }

    if c["clean"]:
        result["exact_mode_match"] = None
    else:
        result["exact_mode_match"] = (
            objection.failure_mode == c["expected_mode"]
        )

    return result


def print_summary(results):
    complete = [r for r in results if "error" not in r]
    flawed = [r for r in complete if not r["clean"]]
    clean = [r for r in complete if r["clean"]]
    severities = [r["severity"] for r in complete]

    print("\n" + "=" * 72)
    print("ADVERSARY CALIBRATION SUMMARY")
    print("=" * 72)
    print(f"completed: {len(complete)}/{len(results)}")

    if flawed:
        exact = sum(bool(r["exact_mode_match"]) for r in flawed)
        print(f"exact failure-mode matches: {exact}/{len(flawed)} "
              f"({exact / len(flawed):.1%})")
        print(f"flawed mean severity: {statistics.mean(r['severity'] for r in flawed):.3f}")

    if clean:
        print(f"clean mean severity:  {statistics.mean(r['severity'] for r in clean):.3f}")
        print(f"clean max severity:   {max(r['severity'] for r in clean):.3f}")

    if severities:
        p50 = quantile(severities, 0.50)
        p85 = quantile(severities, 0.85)
        print(f"\nobserved p50 severity: {p50:.3f}")
        print(f"observed p85 severity: {p85:.3f}")
        print("percentile-rule starting point:")
        print(f"  proceed < {p50:.2f}")
        print(f"  revise  {p50:.2f}–{p85:.2f}")
        print(f"  block   > {p85:.2f}")

        bands = Counter()
        for r in complete:
            s = r["severity"]
            if s < p50:
                bands["proceed"] += 1
            elif s <= p85:
                bands["revise"] += 1
            else:
                bands["block"] += 1
        print(f"band counts: {dict(bands)}")

    print("\nper expected mode:")
    by_mode = defaultdict(list)
    for r in flawed:
        by_mode[r["expected_mode"]].append(r)

    for mode in sorted(by_mode):
        rows = by_mode[mode]
        matches = sum(bool(r["exact_mode_match"]) for r in rows)
        mean_sev = statistics.mean(r["severity"] for r in rows)
        observed = Counter(r["observed_mode"] for r in rows)
        print(f"  {mode:22s} match {matches}/{len(rows)}  "
              f"mean sev {mean_sev:.3f}  observed={dict(observed)}")

    if clean:
        print("\nclean controls observed modes:")
        print(" ", dict(Counter(r["observed_mode"] for r in clean)))

    print("=" * 72)


def write_outputs(results):
    os.makedirs(OUTDIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    jpath = os.path.join(OUTDIR, f"adversary_{ts}.json")
    cpath = os.path.join(OUTDIR, f"adversary_{ts}.csv")

    with open(jpath, "w") as f:
        json.dump(results, f, indent=2, default=str)

    fields = [
        "case_id", "expected_mode", "clean", "observed_mode",
        "severity", "exact_mode_match", "note", "error"
    ]
    with open(cpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k) for k in fields})

    return jpath, cpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N cases")
    ap.add_argument("--case", type=str, default=None,
                    help="run one exact case id")
    args = ap.parse_args()

    cases = build_cases()

    if args.case:
        cases = [c for c in cases if c["case_id"] == args.case]
        if not cases:
            raise SystemExit(f"Unknown case id: {args.case}")
    elif args.limit is not None:
        cases = cases[:args.limit]

    print("Devil's Advocate — adversary calibration")
    print("NO BROKER ORDER PATH EXISTS IN THIS SCRIPT.")
    print(f"cases: {len(cases)}\n")

    results = []

    for i, c in enumerate(cases, start=1):
        print(f"[{i:02d}/{len(cases):02d}] {c['case_id']:24s} "
              f"expected={c['expected_mode']}")
        try:
            r = run_case(c)
            results.append(r)
            print(f"      observed={r['observed_mode']:22s} "
                  f"severity={r['severity']:.2f} "
                  f"match={r['exact_mode_match']}")
        except Exception as e:
            results.append({
                "case_id": c["case_id"],
                "expected_mode": c["expected_mode"],
                "clean": c["clean"],
                "note": c["note"],
                "error": f"{type(e).__name__}: {e}",
            })
            print(f"      ERROR {type(e).__name__}: {e}")

    jpath, cpath = write_outputs(results)
    print_summary(results)

    print(f"\nJSON: {os.path.relpath(jpath, ROOT)}")
    print(f"CSV:  {os.path.relpath(cpath, ROOT)}")


if __name__ == "__main__":
    main()
