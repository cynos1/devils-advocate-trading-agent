"""
check_account.py — first thing to run. Answers today's blocking questions.

    python3 check_account.py

Tells you:
  1. Are you talking to the RIGHT account? (competition, $100k, empty)
  2. Is options trading approved, and at what level?
  3. Can you actually pull an options chain?
  4. Which expiries are available inside the hackathon window?

Run this from the autoinvest_hackathon folder with the COMPETITION account
keys in .env. If it prints the wrong equity, you are pointed at the wrong
account — stop and fix that before anything else.
"""

import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

UNDERLYING = "SPY"        # most liquid options in existence — good for testing
HACKATHON_END = date(2026, 9, 4)


def main():
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("FAIL: no API keys found. Create .env with the COMPETITION "
              "account keys.")
        return 1

    print(f"key ends in ...{key[-4:]}  (confirm this is the competition key)")
    print()

    from alpaca.trading.client import TradingClient
    client = TradingClient(key, secret, paper=True)

    # ---------------------------------------------------------- account
    print("=" * 62)
    print("ACCOUNT")
    print("=" * 62)
    acct = client.get_account()
    print(f"  account number : {acct.account_number}")
    print(f"  equity         : ${float(acct.equity):,.2f}")
    print(f"  cash           : ${float(acct.cash):,.2f}")
    print(f"  status         : {acct.status}")

    positions = client.get_all_positions()
    print(f"  open positions : {len(positions)}")
    for p in positions:
        print(f"      {p.symbol}: {p.qty}")

    if abs(float(acct.equity) - 100_000) > 1000 or positions:
        print("\n  ** This does not look like a fresh $100k competition "
              "account. **")
        print("  ** Check you are using the right keys before continuing. **")

    # ------------------------------------------------------- options level
    print()
    print("=" * 62)
    print("OPTIONS APPROVAL")
    print("=" * 62)
    level = None
    for attr in ("options_approved_level", "options_trading_level"):
        val = getattr(acct, attr, None)
        print(f"  {attr:24}: {val if val is not None else '(not present)'}")
        if val is not None:
            level = int(val)

    if level is None:
        print("\n  Could not read a level. Check the dashboard directly.")
    elif level == 0:
        print("\n  BLOCKED: options not enabled. Enable in the dashboard "
              "before doing anything else today.")
    elif level == 1:
        print("\n  Level 1 — covered calls and cash-secured puts only.")
        print("  Protective puts need Level 2. Request the upgrade if you "
              "want all three strategies.")
    else:
        print(f"\n  Level {level} — all three planned strategies available.")

    # ------------------------------------------------------------- chain
    print()
    print("=" * 62)
    print(f"OPTIONS CHAIN — {UNDERLYING}")
    print("=" * 62)

    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionChainRequest

        data = OptionHistoricalDataClient(key, secret)
        chain = data.get_option_chain(OptionChainRequest(
            underlying_symbol=UNDERLYING))

        print(f"  contracts returned: {len(chain)}")

        # group by expiry so we can see what is available in the window
        expiries = {}
        for sym in chain:
            # OCC symbol: SPY260904C00650000  ->  YYMMDD after the root
            digits = "".join(c for c in sym if c.isdigit())
            if len(digits) >= 6:
                ymd = digits[:6]
                try:
                    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
                    expiries[exp] = expiries.get(exp, 0) + 1
                except ValueError:
                    pass

        today = date.today()
        usable = sorted(e for e in expiries
                        if today <= e <= HACKATHON_END + timedelta(days=10))

        print(f"\n  expiries within reach ({today} to "
              f"{HACKATHON_END + timedelta(days=10)}):")
        for e in usable[:12]:
            inside = "  <-- resolves inside the window" if e <= HACKATHON_END else ""
            print(f"      {e}  ({expiries[e]} contracts){inside}")

        if not any(e <= HACKATHON_END for e in usable):
            print("\n  ** No expiry resolves before the deadline. **")
            print("  ** Positions would still be open at judging. Plan for "
                  "that in the write-up. **")

        # one sample contract so you can see the shape of the data
        sample = next(iter(chain))
        print(f"\n  sample contract: {sample}")
        snap = chain[sample]
        for attr in ("latest_quote", "latest_trade", "implied_volatility",
                     "open_interest"):
            v = getattr(snap, attr, None)
            if v is not None:
                print(f"      {attr}: {v}")

        print("\n  CHAIN ACCESS: working")

    except ImportError as e:
        print(f"  FAIL: {e}")
        print("  Run: python3 -m pip install --upgrade alpaca-py")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        print("\n  If this is a permissions error, options data may need "
              "enabling separately from options trading.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
