"""
options_data.py — find contracts the agent is allowed to consider.

Pulls a chain, applies the eligibility filters from SPEC section 5, and
returns a ranked list of tradeable candidates. Everything rejected is
recorded with a reason, so the journal can show what was screened out and why.

This layer makes no decisions. It only answers: which contracts are even
eligible today?
"""

import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- config

APPROVED_UNDERLYINGS = ("XLE", "XLF", "XLP")

MIN_MID = 0.20            # below this, percentage spread is noise
MIN_PREMIUM = 25.00       # per contract, not worth the transaction below
MAX_SPREAD_PCT = 0.10     # only meaningful once mid clears MIN_MID
MIN_OPEN_INTEREST = 50      # was 100 — left XLF with one eligible contract

MAX_LAST_TRADE_AGE_DAYS = 3      # a quoted contract can still be dead
MIN_DAYS_TO_EXPIRY = 3
MAX_DAYS_TO_EXPIRY = 7

# how far out of the money a call may be and still be worth writing
MAX_MONEYNESS = 0.12        # was 0.08 — was blocking liquid XLF strikes


# ----------------------------------------------------------------- types

@dataclass
class Candidate:
    symbol: str
    underlying: str
    kind: str                 # "call" | "put"
    strike: float
    expiry: str
    days_to_expiry: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    premium: float            # mid * 100
    open_interest: int
    last_trade_age_days: float
    moneyness: float          # (strike - spot) / spot
    implied_volatility: float = 0.0

    def to_dict(self):
        return asdict(self)

    def __repr__(self):
        return (f"{self.symbol}  K={self.strike:.2f}  {self.expiry}  "
                f"prem=${self.premium:,.0f}  spr={self.spread_pct:.1%}  "
                f"OI={self.open_interest:,}")


@dataclass
class ScreenReport:
    """What survived, and why everything else did not."""
    underlying: str
    spot: float
    considered: int = 0
    eligible: list = field(default_factory=list)
    rejected: dict = field(default_factory=dict)   # reason -> count

    def reject(self, reason: str):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def summary(self) -> str:
        parts = [f"{self.underlying} @ ${self.spot:,.2f}: "
                 f"{len(self.eligible)} eligible of {self.considered}"]
        if self.rejected:
            top = sorted(self.rejected.items(), key=lambda x: -x[1])
            parts.append("  rejected: " + ", ".join(
                f"{r} ({n})" for r, n in top))
        return "\n".join(parts)


# ------------------------------------------------------------- utilities

def parse_occ(symbol: str):
    """
    OCC symbol: XLE260904C00064000
                ^^^ root
                   ^^^^^^ YYMMDD
                         ^ C or P
                          ^^^^^^^^ strike in thousandths

    Returns (root, expiry_date, kind, strike) or None.
    """
    i = 0
    while i < len(symbol) and not symbol[i].isdigit():
        i += 1
    root, rest = symbol[:i], symbol[i:]
    if len(rest) < 15:
        return None
    try:
        exp = date(2000 + int(rest[0:2]), int(rest[2:4]), int(rest[4:6]))
    except ValueError:
        return None
    kind = "call" if rest[6].upper() == "C" else "put"
    try:
        strike = int(rest[7:15]) / 1000
    except ValueError:
        return None
    return root, exp, kind, strike


def _age_days(ts) -> float:
    if ts is None:
        return 999.0
    if isinstance(ts, datetime):
        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 86400
    return 999.0


# --------------------------------------------------------------- fetching

class OptionsData:
    """Wraps Alpaca's options endpoints. Open interest needs a second call."""

    def __init__(self, key=None, secret=None):
        key = key or os.environ.get("ALPACA_API_KEY")
        secret = secret or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient

        self.trading = TradingClient(key, secret, paper=True)
        self.stock = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)

    def spot(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest
        t = self.stock.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(t[symbol].price)

    def open_interest_map(self, underlying: str, lo: date, hi: date) -> dict:
        """
        Open interest is NOT on the chain snapshot — it lives on the
        contracts endpoint. Filtering on the snapshot alone silently
        treats every contract as having zero open interest.
        """
        from alpaca.trading.requests import GetOptionContractsRequest
        out = {}
        try:
            resp = self.trading.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_gte=lo,
                expiration_date_lte=hi,
                limit=10_000,
            ))
            for c in (resp.option_contracts or []):
                out[c.symbol] = int(c.open_interest) if c.open_interest else 0
        except Exception:
            pass
        return out

    def screen(self, underlying: str, kind: str = "call",
               spot: float = None) -> ScreenReport:
        """Return eligible contracts for one underlying."""
        from alpaca.data.requests import OptionChainRequest

        if underlying not in APPROVED_UNDERLYINGS:
            raise ValueError(f"{underlying} is not an approved underlying")

        spot = spot if spot is not None else self.spot(underlying)
        report = ScreenReport(underlying=underlying, spot=spot)

        today = date.today()
        lo = today + timedelta(days=MIN_DAYS_TO_EXPIRY)
        hi = today + timedelta(days=MAX_DAYS_TO_EXPIRY)

        oi_map = self.open_interest_map(underlying, lo, hi)
        chain = self.options.get_option_chain(
            OptionChainRequest(underlying_symbol=underlying))

        for sym, snap in chain.items():
            parsed = parse_occ(sym)
            if not parsed:
                continue
            root, exp, ckind, strike = parsed

            if ckind != kind:
                continue

            report.considered += 1

            dte = (exp - today).days
            if dte < MIN_DAYS_TO_EXPIRY or dte > MAX_DAYS_TO_EXPIRY:
                report.reject("expiry_window")
                continue

            q = getattr(snap, "latest_quote", None)
            if not q or not q.bid_price or not q.ask_price:
                report.reject("no_quote")
                continue

            bid, ask = float(q.bid_price), float(q.ask_price)
            mid = (bid + ask) / 2

            if mid < MIN_MID:
                report.reject("mid_below_floor")
                continue

            premium = mid * 100
            if premium < MIN_PREMIUM:
                report.reject("premium_too_small")
                continue

            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            if spread_pct > MAX_SPREAD_PCT:
                report.reject("spread_too_wide")
                continue

            oi = oi_map.get(sym, 0)
            if oi < MIN_OPEN_INTEREST:
                report.reject("open_interest_low")
                continue

            trade = getattr(snap, "latest_trade", None)
            age = _age_days(getattr(trade, "timestamp", None))
            if age > MAX_LAST_TRADE_AGE_DAYS:
                report.reject("last_trade_stale")
                continue

            moneyness = (strike - spot) / spot
            if kind == "call" and not (0 <= moneyness <= MAX_MONEYNESS):
                report.reject("strike_out_of_range")
                continue
            if kind == "put" and not (-MAX_MONEYNESS <= moneyness <= 0):
                report.reject("strike_out_of_range")
                continue

            report.eligible.append(Candidate(
                symbol=sym, underlying=root, kind=kind, strike=strike,
                expiry=exp.isoformat(), days_to_expiry=dte,
                bid=bid, ask=ask, mid=round(mid, 4),
                spread_pct=round(spread_pct, 4),
                premium=round(premium, 2),
                open_interest=oi,
                last_trade_age_days=round(age, 2),
                moneyness=round(moneyness, 4),
                implied_volatility=float(
                    getattr(snap, "implied_volatility", 0) or 0),
            ))

        # most premium first, then tightest spread
        report.eligible.sort(key=lambda c: (-c.premium, c.spread_pct))
        return report


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    data = OptionsData()

    for sym in APPROVED_UNDERLYINGS:
        for kind in ("call", "put"):
            rep = data.screen(sym, kind)
            print("\n" + "=" * 72)
            print(f"{sym} {kind.upper()}S")
            print("=" * 72)
            print(rep.summary())
            if rep.eligible:
                print()
                for c in rep.eligible[:6]:
                    print("  " + repr(c))
            else:
                print("\n  nothing eligible today")
    print()
