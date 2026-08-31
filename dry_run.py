"""
dry_run.py — the whole pipeline, once, with nothing placed.

    python3 dry_run.py           real chain data, mock account, live models
    python3 dry_run.py --offline no network at all, canned proposal

Runs: fetch chain -> screen contracts -> propose -> challenge -> arbitrate
-> (one revision if warranted) -> final ruling. Prints every stage.

Places no orders. Ever. This file has no execution path.
"""

import argparse
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from agent import gate
from agent.broker import MockOptionsBroker
from agent.options_data import OptionsData, APPROVED_UNDERLYINGS
from agent import debate


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="no network; canned candidates and proposal")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    broker = MockOptionsBroker()
    snap = broker.snapshot()

    # ------------------------------------------------------- 1. account
    rule("1. ACCOUNT")
    print(f"  equity      ${snap.equity:,.2f}")
    print(f"  cash        ${snap.cash:,.2f}  "
          f"(${snap.free_cash():,.2f} free)")
    print(f"  market      {'open' if snap.market_open else 'closed'}")
    for sym, n in sorted(snap.shares.items()):
        print(f"  {sym:5}       {n} shares @ ${snap.spots.get(sym, 0):,.2f}  "
              f"= {snap.lots_held(sym)} contracts coverable")
    print(f"  options     {len(snap.option_positions)} open")

    # ----------------------------------------------------- 2. candidates
    rule("2. ELIGIBLE CONTRACTS")

    if args.offline:
        class C:
            def __init__(s, sym, u, k, st, ex, pr, oi=500, sp=0.04, d=8):
                s.symbol=sym; s.underlying=u; s.kind=k; s.strike=st
                s.expiry=ex; s.premium=pr; s.open_interest=oi
                s.spread_pct=sp; s.days_to_expiry=d
        candidates = [
            C("XLE260904C00063000","XLE","call",63.0,"2026-09-04",86.0,167,.058),
            C("XLE260904C00063500","XLE","call",63.5,"2026-09-04",62.0,138,.097),
            C("XLE260904C00065000","XLE","call",65.0,"2026-09-04",28.0,120,.087),
            C("XLE260904P00062000","XLE","put",62.0,"2026-09-04",78.0,3641,.013),
            C("XLF260904C00058500","XLF","call",58.5,"2026-09-04",46.0,939,.066),
            C("XLP260904C00086500","XLP","call",86.5,"2026-09-04",92.0,105,.065),
        ]
        print("  (offline: 6 canned contracts)")
    else:
        data = OptionsData()
        candidates = []
        for sym in APPROVED_UNDERLYINGS:
            for kind in ("call", "put"):
                rep = data.screen(sym, kind)
                candidates.extend(rep.eligible)
                print(f"  {sym} {kind + 's':6} {len(rep.eligible):>3} eligible "
                      f"of {rep.considered}")

    if not candidates:
        print("\n  Nothing eligible. The agent would stand down.")
        return 0

    print(f"\n  {len(candidates)} contracts total. Top by premium:")
    for c in sorted(candidates, key=lambda x: -x.premium)[:5]:
        print(f"    {c.symbol}  {c.kind:4}  ${c.strike:>7.2f}  "
              f"{c.expiry}  ${c.premium:>6,.0f}/contract  "
              f"spr {c.spread_pct:.1%}  OI {c.open_interest:,}")

    # ------------------------------------------------------ 3. preflight
    rule("3. PREFLIGHT")
    pre = gate.preflight(snap, day_start_equity=snap.equity,
                         trades_today=0, project_root=".")
    if not pre.may_run:
        for b in pre.blocks:
            print(f"  BLOCKED  {b}")
        print("\n  The agent would place nothing today.")
        return 0
    print("  clear — the agent may act")

    # -------------------------------------------------------- 4. propose
    rule("4. PROPOSER")
    if args.offline:
        proposal = debate.parse_proposal(
            '{"action":"covered_call","underlying":"XLE",'
            '"contract_symbol":"XLE260904C00063000","strike":63.0,'
            '"expiry":"2026-09-04","contracts":3,"expected_premium":258.0,'
            '"reasoning":"Canned offline proposal, deliberately stating the '
            'total premium instead of the per-contract figure."}',
            candidates=candidates, snapshot=snap)
    else:
        proposal = debate.propose(snap, candidates)

    if proposal.failed or not proposal.is_trade:
        print(f"  {proposal.action}")
        print(f"  {proposal.reasoning}")
        print("\n  Nothing proposed. The agent would stand down.")
        return 0

    print(f"  strategy      {proposal.action}")
    print(f"  contract      {proposal.contract_symbol}")
    print(f"  contracts     {proposal.contracts}")
    print(f"  strike        ${proposal.strike:,.2f}")
    print(f"  expiry        {proposal.expiry}")
    print(f"  premium       ${proposal.expected_premium:,.2f} per contract")
    print(f"  total         ${proposal.total_premium:,.2f}  (computed)")
    print(f"  max loss      ${proposal.max_loss:,.2f}  (computed)")
    print(f"\n  \"{proposal.reasoning}\"")

    if proposal.corrections:
        print("\n  ARITHMETIC CORRECTED BY THE PARSER:")
        for c in proposal.corrections:
            print(f"    - {c}")

    # ------------------------------------------- 5. gate, before the debate
    rule("5. RISK GATE — checked before the debate matters")
    scr = gate.screen(proposal, snap, candidates)
    print(f"  passed: {', '.join(scr.checks_passed)}")
    if scr.vetoes:
        for v in scr.vetoes:
            print(f"  VETO   {v}")
    else:
        print("  no vetoes — the trade is permissible if the debate allows it")

    # ------------------------------------------------------ 6. adversary
    rule("6. ADVERSARY")
    if args.offline:
        objection = debate.parse_objection(
            '{"severity":0.55,"failure_mode":"assignment_risk",'
            '"objection":"The strike sits barely above spot with eight days '
            'left, so assignment is a live possibility.",'
            '"what_would_fix_it":"Move to the $65 strike or halve the size."}')
    else:
        objection = debate.challenge(snap, proposal, candidates)

    print(f"  severity      {objection.severity:.2f}")
    print(f"  failure mode  {objection.failure_mode}")
    print(f"\n  \"{objection.objection}\"")
    if objection.what_would_fix_it:
        print(f"\n  suggested fix: {objection.what_would_fix_it}")
    if objection.failed:
        print("\n  (the adversary failed and was treated as blocking)")

    # ------------------------------------------------------ 7. arbitrate
    rule("7. ARBITER — deterministic code, not a model")
    ruling = gate.arbitrate(proposal, objection, snap, candidates,
                            revision_used=False, block_streak=0)
    print(f"  outcome   {ruling.outcome.upper()}")
    print(f"  {ruling.rationale}")

    # -------------------------------------------------------- 8. revision
    if ruling.outcome == "revise":
        rule("8. REVISION — the one permitted round")
        if args.offline:
            revised = debate.parse_proposal(
                '{"action":"covered_call","underlying":"XLE",'
                '"contract_symbol":"XLE260904C00065000","strike":65.0,'
                '"expiry":"2026-09-04","contracts":2,"expected_premium":28.0,'
                '"reasoning":"Moved two strikes further out and reduced size '
                'to address assignment risk."}',
                candidates=candidates, snapshot=snap)
        else:
            revised = debate.revise(snap, proposal, objection, candidates)

        if revised.is_trade:
            print(f"  now       {revised.contracts}x {revised.contract_symbol}")
            print(f"  strike    ${revised.strike:,.2f} "
                  f"(was ${proposal.strike:,.2f})")
            print(f"  premium   ${revised.expected_premium:,.2f} per contract "
                  f"(was ${proposal.expected_premium:,.2f})")
            print(f"  max loss  ${revised.max_loss:,.2f} "
                  f"(was ${proposal.max_loss:,.2f})")
            print(f"\n  \"{revised.reasoning}\"")
        else:
            print(f"  {revised.action}: {revised.reasoning}")

        rule("9. ADVERSARY, SECOND LOOK")
        if args.offline:
            objection2 = debate.parse_objection(
                '{"severity":0.25,"failure_mode":"none",'
                '"objection":"The revision addresses the assignment concern.",'
                '"what_would_fix_it":""}')
        else:
            objection2 = debate.challenge(snap, revised, candidates)
        print(f"  severity      {objection2.severity:.2f} "
              f"(was {objection.severity:.2f})")
        print(f"  failure mode  {objection2.failure_mode}")
        print(f"\n  \"{objection2.objection}\"")

        rule("10. FINAL RULING")
        ruling = gate.arbitrate(revised, objection2, snap, candidates,
                                revision_used=True, block_streak=0)
        print(f"  outcome   {ruling.outcome.upper()}")
        print(f"  {ruling.rationale}")

    # ----------------------------------------------------------- summary
    rule("WHAT WOULD HAVE HAPPENED")
    if ruling.proposal is not None:
        p = ruling.proposal
        verb = "SELL" if p.action in ("covered_call", "cash_secured_put") else "BUY"
        print(f"  {verb} {p.contracts}x {p.contract_symbol}")
        print(f"  premium   ${p.total_premium:,.2f} total")
        print(f"  max loss  ${p.max_loss:,.2f} "
              f"({p.max_loss / snap.equity:.2%} of equity)")
        if ruling.substituted:
            print(f"  NOTE: substituted by the gate, "
                  f"down from {ruling.original_contracts} contracts")
    else:
        print("  Nothing. No order would be placed today.")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n  ({elapsed:.1f}s)  NO ORDERS WERE PLACED — this script "
          f"has no execution path.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
