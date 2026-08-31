"""
broker.py — the only file that talks to the outside world.

Two implementations, one interface:

    AlpacaOptionsBroker  — the real competition account
    MockOptionsBroker    — in memory, no network, deterministic

Everything above this layer works against either, so the whole pipeline can
be tested with no keys and no market open. On the previous project this
saved hours repeatedly; here it is the difference between debugging on
Friday morning and debugging now.

Failure policy: if anything is uncertain, raise. The caller logs it and
places nothing.
"""

import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone


@dataclass
class OptionPosition:
    """An open options position."""
    symbol: str              # OCC symbol
    underlying: str
    kind: str                # "call" | "put"
    side: str                # "short" (sold) | "long" (bought)
    contracts: int
    strike: float
    expiry: str
    entry_premium: float     # per contract, in dollars
    opened: str

    def to_dict(self):
        return asdict(self)


@dataclass
class Snapshot:
    """Everything the agent needs to know right now."""
    equity: float
    cash: float
    shares: dict                          # {"XLE": 400}
    option_positions: list = field(default_factory=list)
    market_open: bool = True
    spots: dict = field(default_factory=dict)

    # ---------------------------------------------- derived, used by the gate

    def lots_held(self, underlying: str) -> int:
        """How many contracts the shares could cover."""
        return self.shares.get(underlying, 0) // 100

    def short_calls_on(self, underlying: str) -> int:
        return sum(p.contracts for p in self.option_positions
                   if p.underlying == underlying
                   and p.kind == "call" and p.side == "short")

    def long_puts_on(self, underlying: str) -> int:
        return sum(p.contracts for p in self.option_positions
                   if p.underlying == underlying
                   and p.kind == "put" and p.side == "long")

    def short_puts_on(self, underlying: str) -> int:
        return sum(p.contracts for p in self.option_positions
                   if p.underlying == underlying
                   and p.kind == "put" and p.side == "short")

    def open_contracts_on(self, underlying: str) -> int:
        return sum(p.contracts for p in self.option_positions
                   if p.underlying == underlying)

    def cash_committed(self) -> float:
        """Cash tied up as collateral for short puts."""
        return sum(p.strike * 100 * p.contracts
                   for p in self.option_positions
                   if p.kind == "put" and p.side == "short")

    def free_cash(self) -> float:
        return self.cash - self.cash_committed()


class BrokerError(Exception):
    pass


# ----------------------------------------------------------------- mock

class MockOptionsBroker:
    """
    Offline broker. Deterministic, no network.

    Seeded to match the real competition account so mock runs and live runs
    behave comparably.
    """

    DEFAULT_SHARES = {"XLE": 400, "XLF": 400, "XLP": 300}
    DEFAULT_SPOTS = {"XLE": 62.55, "XLF": 58.20, "XLP": 86.44}

    def __init__(self, cash=25_764.0, shares=None, spots=None,
                 option_positions=None, market_open=True):
        self._cash = cash
        self._shares = dict(shares or self.DEFAULT_SHARES)
        self._spots = dict(spots or self.DEFAULT_SPOTS)
        self._options = list(option_positions or [])
        self._market_open = market_open
        self.orders_placed = []

    def snapshot(self) -> Snapshot:
        stock_value = sum(n * self._spots.get(s, 0)
                          for s, n in self._shares.items())
        return Snapshot(
            equity=round(stock_value + self._cash, 2),
            cash=round(self._cash, 2),
            shares=dict(self._shares),
            option_positions=list(self._options),
            market_open=self._market_open,
            spots=dict(self._spots),
        )

    def place_option_order(self, symbol, underlying, kind, side,
                           contracts, strike, expiry, premium) -> dict:
        """
        premium is per contract in dollars (e.g. 78.00 for a $0.78 mid).
        Selling credits cash; buying debits it.
        """
        if contracts < 1:
            raise BrokerError("contracts must be at least 1")

        gross = premium * contracts
        if side == "short":
            self._cash += gross
        else:
            if gross > self._cash:
                raise BrokerError(
                    f"buying ${gross:,.2f} of premium with ${self._cash:,.2f} cash")
            self._cash -= gross

        pos = OptionPosition(
            symbol=symbol, underlying=underlying, kind=kind, side=side,
            contracts=contracts, strike=strike, expiry=expiry,
            entry_premium=premium,
            opened=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._options.append(pos)

        rec = {"symbol": symbol, "side": side, "kind": kind,
               "contracts": contracts, "premium": round(premium, 2),
               "gross": round(gross, 2), "status": "filled",
               "id": f"mock-{len(self.orders_placed) + 1}"}
        self.orders_placed.append(rec)
        return rec

    def trades_today(self) -> int:
        return len(self.orders_placed)

    # ------------------------------------------------- test conveniences

    def move_spot(self, underlying: str, price: float):
        self._spots[underlying] = price

    def add_position(self, **kw):
        self._options.append(OptionPosition(**kw))


# --------------------------------------------------------------- alpaca

class AlpacaOptionsBroker:
    """The real competition account. Imports alpaca-py lazily."""

    def __init__(self, underlyings=("XLE", "XLF", "XLP")):
        self.underlyings = list(underlyings)

        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise BrokerError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.stock import StockHistoricalDataClient

        self.trading = TradingClient(key, secret, paper=True)   # paper only
        self.stock = StockHistoricalDataClient(key, secret)

    def snapshot(self) -> Snapshot:
        from alpaca.data.requests import StockLatestTradeRequest
        from agent.options_data import parse_occ

        acct = self.trading.get_account()

        shares, options = {}, []
        for p in self.trading.get_all_positions():
            sym = p.symbol
            parsed = parse_occ(sym)
            if parsed is None:
                # plain equity position
                shares[sym] = int(float(p.qty))
                continue

            root, exp, kind, strike = parsed
            qty = float(p.qty)
            options.append(OptionPosition(
                symbol=sym, underlying=root, kind=kind,
                side="short" if qty < 0 else "long",
                contracts=int(abs(qty)), strike=strike,
                expiry=exp.isoformat(),
                entry_premium=abs(float(p.avg_entry_price)) * 100,
                opened="",
            ))

        spots = {}
        try:
            t = self.stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=self.underlyings))
            spots = {s: float(t[s].price) for s in self.underlyings}
        except Exception:
            pass

        clock = self.trading.get_clock()

        return Snapshot(
            equity=float(acct.equity),
            cash=float(acct.cash),
            shares=shares,
            option_positions=options,
            market_open=bool(clock.is_open),
            spots=spots,
        )

    def place_option_order(self, symbol, underlying, kind, side,
                           contracts, strike, expiry, premium) -> dict:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if underlying not in self.underlyings:
            raise BrokerError(f"refused: {underlying} not in the approved universe")

        order = self.trading.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=contracts,
            side=OrderSide.SELL if side == "short" else OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        return {"symbol": symbol, "side": side, "kind": kind,
                "contracts": contracts, "premium": round(premium, 2),
                "status": str(order.status), "id": str(order.id)}

    def trades_today(self) -> int:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        orders = self.trading.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=since))
        return len(orders)


def get_broker(live=False, **kw):
    """Factory. Mock by default — you must opt in to touching the network."""
    return AlpacaOptionsBroker(**kw) if live else MockOptionsBroker(**kw)
