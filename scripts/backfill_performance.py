import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

url = "https://paper-api.alpaca.markets/v2/account/portfolio/history"

params = {
    "start": "2026-09-04T00:00:00-04:00",
    "end": "2026-09-29T00:00:00-04:00",
    "timeframe": "1D",
}

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}

response = requests.get(url, headers=headers, params=params, timeout=30)
response.raise_for_status()

history = response.json()

timestamps = history.get("timestamp", [])
equities = history.get("equity", [])
profits = history.get("profit_loss", [])
profit_pcts = history.get("profit_loss_pct", [])

path = Path("data/performance/account_snapshots.json")

if path.exists():
    with path.open() as f:
        data = json.load(f)
else:
    data = {"snapshots": []}

snapshots = data.setdefault("snapshots", [])

# Preserve any existing manually/automatically captured dates.
existing_dates = {s.get("date") for s in snapshots}

added = 0

for i, ts in enumerate(timestamps):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    date = dt.date().isoformat()

    if date in existing_dates:
        continue

    snapshot = {
        "date": date,
        "timestamp": dt.isoformat(),
        "equity": float(equities[i]),
        "cash": None,
        "portfolio_value": float(equities[i]),
        "last_equity": None,
        "starting_equity": 100000.00,
        "profit_loss": float(profits[i]) if i < len(profits) and profits[i] is not None else None,
        "profit_loss_pct": float(profit_pcts[i]) if i < len(profit_pcts) and profit_pcts[i] is not None else None,
        "source": "Alpaca portfolio history backfill"
    }

    snapshots.append(snapshot)
    existing_dates.add(date)
    added += 1

snapshots.sort(key=lambda x: x.get("date", ""))

with path.open("w") as f:
    json.dump(data, f, indent=2)

print(f"Added {added} historical portfolio snapshots.")
print("Dates returned by Alpaca:")
for s in snapshots:
    if "2026-09-04" <= s.get("date", "") <= "2026-09-28":
        print(s["date"], s["equity"])
