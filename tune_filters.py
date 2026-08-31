"""
tune_filters.py — see what each threshold actually costs you.

Rather than guessing, this sweeps a few settings and reports how many
contracts survive under each. Run it, look at the table, then set the
constants in options_data.py from evidence.

    python3 tune_filters.py
"""

import sys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, ".")
from agent import options_data as od


SETTINGS = [
    # label,              min_oi, max_moneyness, max_spread, min_mid
    ("current",              100,          0.08,       0.10,    0.20),
    ("OI 50",                 50,          0.08,       0.10,    0.20),
    ("wider strikes",        100,          0.12,       0.10,    0.20),
    ("wider spread 15%",     100,          0.08,       0.15,    0.20),
    ("OI 50 + strikes .12",   50,          0.12,       0.10,    0.20),
    ("loose all round",       50,          0.12,       0.15,    0.15),
]


def apply(oi, moneyness, spread, mid):
    od.MIN_OPEN_INTEREST = oi
    od.MAX_MONEYNESS = moneyness
    od.MAX_SPREAD_PCT = spread
    od.MIN_MID = mid


def main():
    data = od.OptionsData()

    # fetch spots once so every setting screens the same market
    spots = {s: data.spot(s) for s in od.APPROVED_UNDERLYINGS}
    print()
    for s, p in spots.items():
        print(f"  {s} @ ${p:,.2f}")

    print("\n" + "=" * 76)
    print(f"{'setting':22} {'XLE call':>9} {'XLE put':>9} "
          f"{'XLF call':>9} {'XLF put':>9} {'total':>7}")
    print("=" * 76)

    detail = {}

    for label, oi, mny, spr, mid in SETTINGS:
        apply(oi, mny, spr, mid)
        counts = []
        for sym in od.APPROVED_UNDERLYINGS:
            for kind in ("call", "put"):
                rep = data.screen(sym, kind, spot=spots[sym])
                counts.append(len(rep.eligible))
                detail[(label, sym, kind)] = rep
        print(f"{label:22} {counts[0]:>9} {counts[1]:>9} "
              f"{counts[2]:>9} {counts[3]:>9} {sum(counts):>7}")

    print("=" * 76)

    # what is actually blocking XLF, under the loosest setting
    print("\nWhat blocks XLF under 'loose all round':")
    for kind in ("call", "put"):
        rep = detail[("loose all round", "XLF", kind)]
        top = sorted(rep.rejected.items(), key=lambda x: -x[1])
        skip = {"expiry_window"}
        shown = [f"{r} ({n})" for r, n in top if r not in skip][:5]
        print(f"  XLF {kind:5}: {len(rep.eligible)} eligible — "
              + (", ".join(shown) if shown else "nothing else blocking"))

    # show the contracts that a looser setting would newly admit
    print("\nXLF contracts admitted under 'OI 50 + strikes .12':")
    for kind in ("call", "put"):
        rep = detail[("OI 50 + strikes .12", "XLF", kind)]
        for c in rep.eligible[:6]:
            print(f"  {c!r}")
        if not rep.eligible:
            print(f"  ({kind}s: none)")

    print("\nPick the loosest setting that does not admit contracts you "
          "would be uncomfortable trading.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
