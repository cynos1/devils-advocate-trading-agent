"""
check_liquidity.py — get real open interest, and see what fields exist.

The chain snapshot does not carry open_interest. It lives on the trading
client's option contracts endpoint. This script uses the right source and
also dumps the available fields so you know what you can filter on.

    python3 check_liquidity.py XLE IWM XLF
"""

import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

DEFAULT = ["XLE", "IWM", "XLF", "SPY"]


def main():
    symbols = sys.argv[1:] or DEFAULT

    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("No API keys in .env")
        return 1

    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest, OptionChainRequest

    trading = TradingClient(key, secret, paper=True)
    stock = StockHistoricalDataClient(key, secret)
    optdata = OptionHistoricalDataClient(key, secret)

    lo = date.today() + timedelta(days=5)
    hi = date.today() + timedelta(days=21)

    # ---- one-time: what fields does a chain snapshot actually have? ----
    print("=" * 74)
    print("AVAILABLE SNAPSHOT FIELDS (so you know what you can filter on)")
    print("=" * 74)
    try:
        sample_chain = optdata.get_option_chain(
            OptionChainRequest(underlying_symbol=symbols[0]))
        sample = next(iter(sample_chain.values()))
        for f in sorted(vars(sample).keys()):
            v = getattr(sample, f, None)
            kind = type(v).__name__ if v is not None else "None"
            print(f"  {f:24} {kind}")
    except Exception as e:
        print(f"  could not inspect: {type(e).__name__}: {e}")

    # ---- per symbol: real open interest from the contracts endpoint ----
    for sym in symbols:
        print()
        print("=" * 74)
        print(f"{sym}")
        print("=" * 74)

        try:
            t = stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=sym))
            price = float(t[sym].price)
        except Exception as e:
            print(f"  price failed: {type(e).__name__}")
            continue

        print(f"  spot ${price:,.2f}   one lot (100 sh) ${price * 100:,.0f}")

        # contracts endpoint carries open_interest
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[sym],
                expiration_date_gte=lo,
                expiration_date_lte=hi,
                strike_price_gte=str(round(price * 0.97, 2)),
                strike_price_lte=str(round(price * 1.05, 2)),
                type="call",
                limit=100,
            )
            resp = trading.get_option_contracts(req)
            contracts = resp.option_contracts or []
        except Exception as e:
            print(f"  contracts endpoint failed: {type(e).__name__}: {e}")
            continue

        if not contracts:
            print("  no contracts returned in that strike/expiry range")
            continue

        # quotes for the same contracts, to pair OI with spread
        quotes = {}
        try:
            chain = optdata.get_option_chain(OptionChainRequest(underlying_symbol=sym))
            quotes = chain
        except Exception:
            pass

        print(f"\n  {'contract':24} {'strike':>8} {'expiry':>11} "
              f"{'OI':>8} {'spread':>8} {'mid':>8}")
        print("  " + "-" * 70)

        rows = []
        for c in contracts:
            oi = int(c.open_interest) if c.open_interest else 0
            snap = quotes.get(c.symbol)
            spread_pct = mid = None
            if snap is not None:
                q = getattr(snap, "latest_quote", None)
                if q and q.bid_price and q.ask_price:
                    b, a = float(q.bid_price), float(q.ask_price)
                    mid = (a + b) / 2
                    if mid > 0:
                        spread_pct = (a - b) / mid
            rows.append((c, oi, spread_pct, mid))

        # show the most liquid first
        rows.sort(key=lambda r: -r[1])
        for c, oi, spread_pct, mid in rows[:8]:
            sp = f"{spread_pct:.1%}" if spread_pct is not None else "n/a"
            md = f"${mid:,.2f}" if mid is not None else "n/a"
            print(f"  {c.symbol:24} {float(c.strike_price):>8.2f} "
                  f"{str(c.expiration_date):>11} {oi:>8,} {sp:>8} {md:>8}")

        best = rows[0] if rows else None
        if best:
            c, oi, spread_pct, mid = best
            ok_oi = oi >= 100
            ok_sp = spread_pct is not None and spread_pct <= 0.10
            print(f"\n  most liquid: OI {oi:,} "
                  f"({'passes' if ok_oi else 'FAILS'} the 100 minimum), "
                  f"spread {spread_pct:.1%} "
                  f"({'passes' if ok_sp else 'FAILS'} the 10% limit)"
                  if spread_pct is not None else "")
            if mid:
                print(f"  premium on one contract: ~${mid * 100:,.0f}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
