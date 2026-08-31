"""
screen_underlyings.py — pick what to hold, from data rather than memory.

    python3 screen_underlyings.py

For each candidate it reports:
  - share price, and what 100 shares costs as a share of $100k
  - how many contracts you could realistically run
  - near-the-money open interest and bid-ask spread on a short expiry
  - whether it passes the spec's liquidity filters

Run this before seeding the account. The choice is awkward to change later.
"""

import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

CANDIDATES = ["SPY", "IWM", "XLF", "XLE", "EFA", "GLD", "DIA"]

EQUITY = 100_000
MAX_SPREAD_PCT = 0.10      # spec: bid-ask within 10% of mid
MIN_OPEN_INTEREST = 100    # spec: at least 100 open interest


def parse_expiry(occ_symbol: str):
    """OCC symbol: IWM260904C00245000 -> date(2026, 9, 4)"""
    digits = "".join(c for c in occ_symbol if c.isdigit())
    if len(digits) < 6:
        return None
    try:
        return date(2000 + int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError:
        return None


def main():
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("No API keys in .env")
        return 1

    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest, OptionChainRequest

    stock = StockHistoricalDataClient(key, secret)
    opt = OptionHistoricalDataClient(key, secret)

    # target expiry: roughly a week and a half out, inside the spec window
    target_lo = date.today() + timedelta(days=5)
    target_hi = date.today() + timedelta(days=21)

    print(f"\nScreening for expiries between {target_lo} and {target_hi}")
    print(f"Account equity assumed: ${EQUITY:,}\n")

    rows = []

    for sym in CANDIDATES:
        try:
            trade = stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=sym))
            price = float(trade[sym].price)
        except Exception as e:
            print(f"{sym}: price lookup failed — {type(e).__name__}")
            continue

        lot_cost = price * 100
        max_lots = int((EQUITY * 0.5) // lot_cost)   # half the account in stock

        try:
            chain = opt.get_option_chain(OptionChainRequest(underlying_symbol=sym))
        except Exception as e:
            print(f"{sym}: chain failed — {type(e).__name__}")
            continue

        # find calls near the money in the target expiry window
        best = None
        for contract, snap in chain.items():
            exp = parse_expiry(contract)
            if not exp or not (target_lo <= exp <= target_hi):
                continue
            if "C" not in contract[-9:]:      # calls only
                continue

            q = getattr(snap, "latest_quote", None)
            if not q or not q.bid_price or not q.ask_price:
                continue

            # strike sits in the last 8 digits, in thousandths
            try:
                strike = int(contract[-8:]) / 1000
            except ValueError:
                continue

            moneyness = abs(strike - price) / price
            if moneyness > 0.05:              # within 5% of spot
                continue

            mid = (float(q.bid_price) + float(q.ask_price)) / 2
            if mid <= 0:
                continue
            spread_pct = (float(q.ask_price) - float(q.bid_price)) / mid
            oi = getattr(snap, "open_interest", None)
            oi = int(oi) if oi else 0

            score = (moneyness, spread_pct)
            if best is None or score < best["score"]:
                best = {"score": score, "contract": contract, "strike": strike,
                        "expiry": exp, "mid": mid, "spread_pct": spread_pct,
                        "oi": oi}

        if not best:
            print(f"{sym}: no near-the-money contract found in window")
            continue

        passes = (best["spread_pct"] <= MAX_SPREAD_PCT
                  and best["oi"] >= MIN_OPEN_INTEREST
                  and max_lots >= 2)

        rows.append({
            "symbol": sym, "price": price, "lot_cost": lot_cost,
            "max_lots": max_lots, "premium": best["mid"] * 100,
            "spread_pct": best["spread_pct"], "oi": best["oi"],
            "expiry": best["expiry"], "strike": best["strike"],
            "passes": passes,
        })

    if not rows:
        print("Nothing screened successfully.")
        return 1

    print("=" * 78)
    print(f"{'sym':5} {'price':>9} {'100sh':>10} {'lots':>5} "
          f"{'premium':>9} {'spread':>8} {'OI':>7}  verdict")
    print("=" * 78)

    for r in sorted(rows, key=lambda x: (not x["passes"], x["lot_cost"])):
        verdict = "OK" if r["passes"] else "fails filters"
        print(f"{r['symbol']:5} ${r['price']:>8,.2f} ${r['lot_cost']:>9,.0f} "
              f"{r['max_lots']:>5} ${r['premium']:>8,.0f} "
              f"{r['spread_pct']:>7.1%} {r['oi']:>7,}  {verdict}")

    print("=" * 78)
    print("\n  price    — share price")
    print("  100sh    — cost of one round lot (one contract's worth)")
    print("  lots     — round lots affordable with half the account")
    print("  premium  — approx income from one near-the-money call")
    print("  spread   — bid-ask as % of mid (spec limit 10%)")
    print("  OI       — open interest (spec minimum 100)")
    print("\nWant: several lots affordable, tight spread, high OI.")
    print("A single lot costing most of the account gives the agent")
    print("nothing to decide.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
