"""
seed_account.py — one-off. Buys the shares the agent will work with.

Run this ONCE, before the hackathon starts. It buys whole batches of 100
shares, because options contracts only work in batches of 100. You cannot
sell a contract against 47 shares.

    python3 seed_account.py            show what it would buy
    python3 seed_account.py --execute  actually buy

Default plan:
    400 shares XLE   (four contracts' worth)
    400 shares XLF   (four contracts' worth)
    ~$52,000 left in cash, for cash-secured puts

The agent itself can never place an order this large. Building the starting
position is a human decision made once; managing it is the agent's job.
"""

import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# symbol -> number of round lots (1 lot = 100 shares = 1 contract's worth)
PLAN = {
    "XLP": 3,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true", help="place the orders")
    args = p.parse_args()

    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("No API keys in .env")
        return 1

    print(f"key ends in ...{key[-4:]}  — confirm this is the COMPETITION key\n")

    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    trading = TradingClient(key, secret, paper=True)
    stock = StockHistoricalDataClient(key, secret)

    acct = trading.get_account()
    positions = trading.get_all_positions()

    print(f"account  : {acct.account_number}")
    print(f"equity   : ${float(acct.equity):,.2f}")
    print(f"cash     : ${float(acct.cash):,.2f}")
    print(f"positions: {len(positions)}")

    if positions:
        print("\n  ** This account already holds positions: **")
        for pos in positions:
            print(f"      {pos.symbol}: {pos.qty}")
        print("  ** Seeding is meant for an empty account. Review before "
              "continuing. **")

    # ------------------------------------------------------------ prices
    symbols = list(PLAN)
    trades = stock.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbols))
    prices = {s: float(trades[s].price) for s in symbols}

    print("\n" + "=" * 62)
    print("PLAN")
    print("=" * 62)

    total = 0.0
    orders = []
    for sym, lots in PLAN.items():
        shares = lots * 100
        cost = shares * prices[sym]
        total += cost
        orders.append((sym, shares, cost))
        print(f"  BUY {shares:>5} {sym:5} @ ~${prices[sym]:>7,.2f}  "
              f"= ${cost:>10,.2f}   ({lots} contract{'s' if lots > 1 else ''} "
              f"worth)")

    cash_after = float(acct.cash) - total
    print(f"\n  {'total':>11}                    ${total:>10,.2f}")
    print(f"  {'cash left':>11}                    ${cash_after:>10,.2f}")

    if cash_after < 0:
        print("\n  NOT ENOUGH CASH. Reduce the lots in PLAN.")
        return 1

    print(f"\n  Cash left over is collateral for cash-secured puts.")
    print(f"  At ~${min(prices.values()):.0f}/share that covers roughly "
          f"{int(cash_after // (min(prices.values()) * 100))} put contracts.")

    if not args.execute:
        print("\nDry run. Add --execute to actually buy.\n")
        return 0

    clock = trading.get_clock()
    if not clock.is_open:
        print("\nMarket is closed. Orders will queue until the next open.")

    print("\nPlacing orders...\n")
    for sym, shares, _ in orders:
        try:
            o = trading.submit_order(MarketOrderRequest(
                symbol=sym,
                qty=shares,               # whole shares, not dollars
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"  submitted  {shares:>5} {sym:5}  status={o.status}")
        except Exception as e:
            print(f"  FAILED     {sym}: {type(e).__name__}: {e}")

    print("\nDone. Confirm fills in the Alpaca dashboard.")
    print("Once filled, re-run check_account.py to verify the positions.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
