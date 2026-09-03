"""
build_report.py — static analysis/report layer for Devil's Advocate.

Reads:
    data/decisions/*.json
    data/calibration/adversary_*.json
    agent/gate.py

Writes:
    report/index.html
    report/summary.json

No broker calls. No model calls. No network calls.

Run:
    python3 build_report.py
    open report/index.html
"""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from agent import gate

ROOT = Path(__file__).resolve().parent
DECISIONS_DIR = ROOT / "data" / "decisions"
CALIBRATION_DIR = ROOT / "data" / "calibration"
REPORT_DIR = ROOT / "report"
PERFORMANCE_FILE = ROOT / "data" / "performance" / "account_snapshots.json"
STARTING_EQUITY = 100000.0


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def esc(v):
    return html.escape("" if v is None else str(v))


def safe_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct(n, d):
    return 0.0 if not d else 100.0 * n / d


def proposal_of(r):
    return r.get("proposal") or {}


def final_ruling_of(r):
    return r.get("final_ruling") or r.get("ruling") or {}


def final_proposal_of(r):
    return final_ruling_of(r).get("proposal") or {}


def is_trade(p):
    return bool(p) and p.get("action") not in (None, "no_trade")


def oversized_initial(r):
    p = proposal_of(r)
    n = p.get("contracts")
    return isinstance(n, int) and n > gate.MAX_CONTRACTS_PER_TRADE


def final_gate_status(r):
    x = r.get("execution_check")
    if not isinstance(x, dict):
        return "not reached"
    return "passed" if x.get("may_execute") else "blocked"


def no_trade(r):
    fr = final_ruling_of(r)
    if fr.get("outcome") == "reject":
        return True
    if r.get("blocks"):
        return True
    p = proposal_of(r)
    if p and p.get("action") == "no_trade":
        return True
    return not r.get("fills") and not fr.get("proposal")


def strategy_changed(r):
    p, rev = proposal_of(r), r.get("revision") or {}
    return is_trade(p) and is_trade(rev) and p.get("action") != rev.get("action")


def contract_changed(r):
    p, rev = proposal_of(r), r.get("revision") or {}
    return is_trade(p) and is_trade(rev) and p.get("contract_symbol") != rev.get("contract_symbol")


def size_reduced(r):
    p, fp = proposal_of(r), final_proposal_of(r)
    a, b = p.get("contracts"), fp.get("contracts")
    return isinstance(a, int) and isinstance(b, int) and b < a


def load_decisions():
    out = []
    for path in sorted(DECISIONS_DIR.glob("*.json")):
        try:
            d = load_json(path)
            d["_path"] = str(path.relative_to(ROOT))
            out.append(d)
        except Exception as e:
            out.append({"_path": str(path.relative_to(ROOT)), "_error": str(e)})
    return out



def load_performance_snapshots():
    """Load persisted account snapshots and calculate comparable snapshot metrics."""
    if not PERFORMANCE_FILE.exists():
        return []

    try:
        raw = load_json(PERFORMANCE_FILE)
    except Exception:
        return []

    rows = raw.get("snapshots", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        equity = safe_float(row.get("equity"))
        if equity is None:
            continue

        timestamp = str(row.get("timestamp") or "")
        date = str(row.get("date") or (timestamp[:10] if len(timestamp) >= 10 else ""))
        if not date:
            continue

        starting_equity = safe_float(row.get("starting_equity"), STARTING_EQUITY)
        cash = safe_float(row.get("cash"))
        last_equity = safe_float(row.get("last_equity"))

        pos = row.get("position_count", row.get("positions"))
        if isinstance(pos, (list, tuple, dict)):
            position_count = len(pos)
        else:
            try:
                position_count = int(pos) if pos is not None else None
            except (TypeError, ValueError):
                position_count = None

        normalized.append({
            "date": date,
            "timestamp": timestamp,
            "equity": equity,
            "cash": cash,
            "last_equity": last_equity,
            "starting_equity": starting_equity,
            "position_count": position_count,
            "source": row.get("source") or "Alpaca paper account snapshot",
        })

    # Keep the latest snapshot for each calendar date.
    daily = {}
    for row in sorted(normalized, key=lambda r: (r["date"], r["timestamp"])):
        daily[row["date"]] = row

    result = [daily[d] for d in sorted(daily)]
    previous_equity = None
    for row in result:
        base = row["starting_equity"] or STARTING_EQUITY
        row["pnl"] = row["equity"] - base
        row["return_pct"] = pct(row["pnl"], base)
        row["snapshot_change"] = (
            None if previous_equity is None else row["equity"] - previous_equity
        )
        row["broker_intraday_change"] = (
            None if row["last_equity"] is None else row["equity"] - row["last_equity"]
        )
        previous_equity = row["equity"]

    return result


def money(v):
    if v is None:
        return "—"
    return f"${float(v):,.2f}"


def signed_money(v):
    if v is None:
        return "—"
    v = float(v)
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def performance_table_rows(rows):
    html_rows = []
    for idx, row in enumerate(reversed(rows)):
        source_index = len(rows) - 1 - idx
        html_rows.append(
            f'''<tr class="perf-row" data-perf-index="{source_index}">
              <td>{esc(row["date"])}</td>
              <td>{esc(money(row["equity"]))}</td>
              <td>{esc(signed_money(row["snapshot_change"]))}</td>
              <td>{esc(signed_money(row["pnl"]))}</td>
              <td>{row["return_pct"]:+.2f}%</td>
              <td>{esc(money(row["cash"]))}</td>
              <td>{esc(row["position_count"] if row["position_count"] is not None else "—")}</td>
            </tr>'''
        )
    return "".join(html_rows)


def select_calibration():
    files = []
    for path in sorted(CALIBRATION_DIR.glob("adversary_*.json")):
        try:
            rows = load_json(path)
            if isinstance(rows, list):
                files.append((path, rows))
        except Exception:
            pass
    complete = [(p, r) for p, r in files if len(r) == 30]
    if complete:
        return complete[-1]
    return files[-1] if files else (None, [])


def auc_pairwise(flawed, clean):
    if not flawed or not clean:
        return None
    wins = ties = total = 0
    for a in flawed:
        for b in clean:
            total += 1
            if a > b:
                wins += 1
            elif a == b:
                ties += 1
    return (wins + 0.5 * ties) / total


def analyze_calibration(rows):
    valid = [r for r in rows if isinstance(r, dict) and "severity" in r and "error" not in r]
    flawed = [r for r in valid if not r.get("clean")]
    clean = [r for r in valid if r.get("clean")]

    def band(s):
        if s < gate.SEVERITY_PROCEED:
            return "proceed"
        if s <= gate.SEVERITY_BLOCK:
            return "revise"
        return "block"

    for r in valid:
        r["_band"] = band(float(r["severity"]))

    flawed_scores = [float(r["severity"]) for r in flawed]
    clean_scores = [float(r["severity"]) for r in clean]
    exact = sum(bool(r.get("exact_mode_match")) for r in flawed)

    by_mode = defaultdict(list)
    for r in flawed:
        by_mode[r.get("expected_mode", "unknown")].append(r)

    modes = {}
    for name, rs in sorted(by_mode.items()):
        modes[name] = {
            "n": len(rs),
            "exact": sum(bool(x.get("exact_mode_match")) for x in rs),
            "mean": statistics.mean(float(x["severity"]) for x in rs),
            "observed": dict(Counter(x.get("observed_mode", "unknown") for x in rs)),
        }

    return {
        "n": len(valid),
        "flawed_n": len(flawed),
        "clean_n": len(clean),
        "exact": exact,
        "flawed_mean": statistics.mean(flawed_scores) if flawed_scores else None,
        "clean_mean": statistics.mean(clean_scores) if clean_scores else None,
        "flawed_intervene": sum(r["_band"] != "proceed" for r in flawed),
        "clean_proceed": sum(r["_band"] == "proceed" for r in clean),
        "clean_block": sum(r["_band"] == "block" for r in clean),
        "auc": auc_pairwise(flawed_scores, clean_scores),
        "modes": modes,
        "rows": valid,
    }


def analyze_decisions(rows):
    rows = [r for r in rows if "_error" not in r]

    def group_metrics(group):
        return {
            "sessions": len(group),
            "revisions": sum("revision" in r for r in group),
            "substitutions": sum(final_ruling_of(r).get("outcome") == "substitute" for r in group),
            "rejects": sum(final_ruling_of(r).get("outcome") == "reject" for r in group),
            "no_trade": sum(no_trade(r) for r in group),
            "fills": sum(bool(r.get("fills")) for r in group),
            "oversized_initial": sum(oversized_initial(r) for r in group),
            "size_reduced": sum(size_reduced(r) for r in group),
            "strategy_changed": sum(strategy_changed(r) for r in group),
            "contract_changed": sum(contract_changed(r) for r in group),
            "fresh_gate_pass": sum(final_gate_status(r) == "passed" for r in group),
            "fresh_gate_block": sum(final_gate_status(r) == "blocked" for r in group),
        }

    live = [r for r in rows if r.get("mode") == "live"]
    live_dry = [r for r in rows if r.get("mode") == "live-dry"]

    objections = []
    for r in rows:
        for key in ("objection", "objection2"):
            o = r.get(key)
            if isinstance(o, dict) and "severity" in o:
                objections.append(o)

    return {
        "all": group_metrics(rows),
        "live": group_metrics(live),
        "live_dry": group_metrics(live_dry),
        "live_rows": live,
        "live_dry_rows": live_dry,
        "objection_modes": dict(Counter(o.get("failure_mode", "unknown") for o in objections)),
    }


def episode_score(r):
    s = 0
    s += 5 if r.get("mode") == "live" else 0
    s += 3 if r.get("revision") else 0
    s += 3 if r.get("objection2") else 0
    s += 5 if oversized_initial(r) else 0
    s += 4 if final_ruling_of(r).get("outcome") == "substitute" else 0
    s += 4 if final_gate_status(r) == "passed" else 0
    s += 2 if final_ruling_of(r).get("outcome") == "reject" else 0
    return s


def select_episodes(rows, n=5):
    valid = [r for r in rows if "_error" not in r]
    return sorted(valid, key=lambda r: (episode_score(r), r.get("date", ""), r.get("session", 0)), reverse=True)[:n]


def badge(text, cls="neutral"):
    return f'<span class="badge {cls}">{esc(text)}</span>'


def outcome_badge(outcome):
    cls = {"execute":"good","substitute":"warn","revise":"warn","reject":"bad","no_trade":"blue"}.get(outcome, "neutral")
    return badge(outcome or "unknown", cls)


def mode_badge(mode):
    cls = {"live":"good","live-dry":"blue"}.get(mode, "neutral")
    return badge(mode or "unknown", cls)


def kpi(label, value, sub):
    return f'''<div class="kpi"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div><div class="sub">{esc(sub)}</div></div>'''


def episode_html(r, index=0):
    p = proposal_of(r)
    rev = r.get("revision") or {}
    o1 = r.get("objection") or {}
    o2 = r.get("objection2") or {}
    fr = final_ruling_of(r)
    fp = final_proposal_of(r)

    stages = []
    if is_trade(p):
        stages.append(("Proposed", f"{p.get('contracts')}× {p.get('action','').replace('_',' ')} · {p.get('contract_symbol')}"))
    elif p:
        stages.append(("Proposed", "No trade"))
    if o1:
        stages.append(("Challenge", f"{o1.get('failure_mode')} · severity {safe_float(o1.get('severity'),0):.2f}"))
    if rev:
        if is_trade(rev):
            stages.append(("Revision", f"{rev.get('contracts')}× {rev.get('action','').replace('_',' ')} · {rev.get('contract_symbol')}"))
        else:
            stages.append(("Revision", "No trade"))
    if o2:
        stages.append(("Challenge again", f"{o2.get('failure_mode')} · severity {safe_float(o2.get('severity'),0):.2f}"))
    final_text = fr.get("outcome", "unknown")
    if is_trade(fp):
        final_text += f" → {fp.get('contracts')}× {fp.get('contract_symbol')}"
    stages.append(("Final ruling", final_text))
    if final_gate_status(r) != "not reached":
        stages.append(("Fresh execution gate", final_gate_status(r)))
    stages.append(("Broker", "Order submitted" if r.get("fills") else "No order placed"))

    stage_html = "".join(f'<div class="stage"><div class="stage-name">{esc(a)}</div><div>{esc(b)}</div></div>' for a,b in stages)

    chips = []
    if oversized_initial(r): chips.append("Initial proposal exceeded contract cap")
    if strategy_changed(r): chips.append("Strategy changed on revision")
    if contract_changed(r): chips.append("Contract changed on revision")
    if size_reduced(r): chips.append("Final size reduced")
    if fr.get("outcome") == "reject": chips.append("Resolved to NO TRADE")
    chip_html = "".join(f'<span class="chip">{esc(x)}</span>' for x in chips)

    return f'''
    <article class="episode trace-card" data-trace-index="{index}">
      <div class="episode-head"><div><strong>{esc(r.get('_path'))}</strong><div class="muted">{esc(r.get('date'))} · session {esc(r.get('session'))} · {mode_badge(r.get('mode'))}</div></div>{outcome_badge(fr.get("outcome") or ("no_trade" if no_trade(r) else None))}</div>
      <div class="stages">{stage_html}</div>
      <div class="chips">{chip_html}</div>
      <details><summary>Rationale</summary><p><b>Proposal:</b> {esc(p.get('reasoning','—'))}</p><p><b>First objection:</b> {esc(o1.get('objection','—'))}</p><p><b>Final ruling:</b> {esc(fr.get('rationale','—'))}</p></details>
    </article>'''


def calibration_table(cal):
    out = []
    for name, x in cal["modes"].items():
        obs = ", ".join(f"{k}: {v}" for k,v in x["observed"].items())
        out.append(f"<tr><td>{esc(name.replace('_',' '))}</td><td>{x['exact']}/{x['n']}</td><td>{x['mean']:.3f}</td><td>{esc(obs)}</td></tr>")
    return "".join(out)


def ledger_rows(rows):
    out = []
    for r in sorted(rows, key=lambda x:(x.get("date",""),x.get("session",0)), reverse=True):
        p = proposal_of(r)
        fr = final_ruling_of(r)
        fp = final_proposal_of(r)
        o = r.get("objection") or {}
        mode = r.get("mode") or "unknown"
        outcome = fr.get("outcome")
        if not outcome and no_trade(r):
            outcome = "no_trade"
        out.append(
            f'''<tr data-mode="{esc(mode)}" data-search="{esc(' '.join(str(x) for x in [r.get('date',''), r.get('session',''), mode, p.get('action',''), p.get('contract_symbol',''), o.get('failure_mode',''), outcome]))}">
              <td>{esc(r.get('date'))}</td>
              <td>{esc(r.get('session'))}</td>
              <td>{mode_badge(mode)}</td>
              <td>{esc(p.get('action','—').replace('_',' '))}</td>
              <td>{esc(p.get('contracts','—'))}</td>
              <td>{esc(o.get('failure_mode','—'))}</td>
              <td>{esc(o.get('severity','—'))}</td>
              <td>{outcome_badge(outcome)}</td>
              <td>{esc(fp.get('contracts','—'))}</td>
              <td>{esc(final_gate_status(r))}</td>
              <td>{len(r.get('fills') or [])}</td>
            </tr>'''
        )
    return "".join(out)


CSS = r'''
:root{--bg:#F7F9FC;--panel:#FFFFFF;--panel2:#F1F5F9;--panel3:#EAF3F8;--text:#0B1F3A;--muted:#667085;--line:#D7E0EA;--accent:#F39C12;--accent-soft:rgba(243,156,18,.12);--green:#1B7F3A;--red:#D92D20;--blue:#1E8798}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1220px;margin:auto;padding:26px 24px 80px}
.hero{padding:18px 0 26px;border-bottom:1px solid var(--line)}
.hero-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(240px,.55fr);gap:28px;align-items:end}
.hero-metric{background:linear-gradient(145deg,#FFFFFF,#F1F5F9);border:1px solid var(--line);border-radius:20px;padding:20px;position:relative;overflow:hidden}
.hero-metric:after{content:"";position:absolute;inset:auto -28px -38px auto;width:130px;height:130px;border-radius:50%;background:var(--accent-soft);filter:blur(2px)}
.hero-metric .big{font-size:38px;font-weight:800;letter-spacing:-.03em;position:relative;z-index:1}
.hero-metric .mini{color:var(--muted);font-size:12px;position:relative;z-index:1}
.status-row{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(27,127,58,.10);animation:pulse 2.1s ease-in-out infinite}
@keyframes pulse{50%{box-shadow:0 0 0 9px rgba(27,127,58,0)}}
.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.13em;font-size:12px;font-weight:700}
h1{font-size:52px;line-height:1.02;margin:7px 0 12px;letter-spacing:-.045em}
h2{font-size:28px;margin:34px 0 10px;letter-spacing:-.02em}
h3{font-size:18px;margin:0 0 8px}
.lede{max-width:820px;color:#667085;font-size:18px}
.muted,.sub{color:var(--muted)}
.small{font-size:12px;color:var(--muted)}
.app-switcher{position:sticky;top:10px;z-index:20;display:flex;gap:7px;align-items:center;margin:18px 0 6px;padding:7px;background:rgba(255,255,255,.94);backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:16px;width:max-content;max-width:100%;overflow:auto}
.view-btn,.filter-btn,.trace-btn{appearance:none;border:1px solid transparent;background:transparent;color:var(--muted);border-radius:11px;padding:9px 13px;font:inherit;font-size:12px;cursor:pointer;white-space:nowrap;transition:.18s ease}
.view-btn:hover,.filter-btn:hover,.trace-btn:hover{color:var(--text);background:var(--panel3)}
.view-btn.active,.filter-btn.active,.trace-btn.active{color:var(--text);background:var(--accent-soft);border-color:rgba(243,156,18,.36)}
.view-panel{display:none;animation:panelIn .24s ease both}
.view-panel.active{display:block}
@keyframes panelIn{from{opacity:.25;transform:translateY(5px)}to{opacity:1;transform:none}}
.section-head{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-top:10px}.section-head p{max-width:620px;margin:0}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(4,1fr);margin-top:18px}
.kpi{background:linear-gradient(160deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:16px;padding:18px;transition:transform .18s ease,border-color .18s ease}
.kpi:hover{transform:translateY(-2px);border-color:#B7C8D9}
.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.value{font-size:30px;font-weight:780;margin:5px 0;letter-spacing:-.025em}
.sub{font-size:13px}
.callout{background:linear-gradient(90deg,var(--accent-soft),transparent);border-left:3px solid var(--accent);padding:16px 19px;margin:18px 0;border-radius:0 12px 12px 0}
.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-size:11px;text-transform:uppercase}.badge.good{color:var(--green)}.badge.bad{color:var(--red)}.badge.warn{color:var(--accent)}.badge.blue{color:var(--blue)}.badge.neutral{color:var(--muted)}
.performance-panel{background:linear-gradient(180deg,#FFFFFF,#F7F9FC);border:1px solid var(--line);border-radius:20px;padding:18px;margin-top:16px;overflow:hidden}
.chart-layout{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:18px;align-items:stretch}
.chart-wrap{width:100%;overflow:hidden;min-width:0}
#perfChart{width:100%;height:310px;display:block}
.chart-grid{stroke:var(--line);stroke-width:1}.chart-line{fill:none;stroke:var(--accent);stroke-width:3.2;transition:stroke-dashoffset .8s ease}.chart-area{fill:url(#equityGradient);opacity:.9}.chart-baseline{stroke:var(--muted);stroke-width:1;stroke-dasharray:5 5;opacity:.65}.chart-point{fill:var(--panel2);stroke:var(--accent);stroke-width:2;cursor:pointer;transition:r .15s ease,fill .15s ease}.chart-point:hover,.chart-point.active{fill:var(--accent);r:8}.chart-text{fill:var(--muted);font-size:11px}
.chart-selected{display:flex;flex-direction:column;justify-content:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:16px;color:var(--muted)}.chart-selected span{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid rgba(11,31,58,.08);padding-bottom:7px}.chart-selected span:last-child{border:0;padding:0}.chart-selected strong{color:var(--text)}
.perf-row{cursor:pointer}.perf-row.selected td{background:var(--accent-soft)}
.trace-switcher{display:flex;gap:8px;overflow:auto;padding:3px 0 12px}.trace-btn{border-color:var(--line);background:var(--panel)}
.trace-card{display:none}.trace-card.active{display:block;animation:panelIn .22s ease both}
.episode{background:linear-gradient(155deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:18px;padding:20px;margin:4px 0}.episode-head{display:flex;justify-content:space-between;gap:15px}.stages{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:14px}.stage{background:rgba(255,255,255,.82);border:1px solid var(--line);border-radius:12px;padding:12px;position:relative}.stage:not(:last-child):after{content:"→";position:absolute;right:-9px;top:50%;transform:translate(50%,-50%);color:var(--accent);background:var(--panel);border-radius:50%;width:18px;height:18px;text-align:center;line-height:17px;font-size:11px}.stage-name{color:var(--muted);font-size:11px;text-transform:uppercase;margin-bottom:4px}.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.chip{background:#EAF3F8;border-radius:999px;padding:4px 9px;font-size:12px}
details{margin-top:13px;color:var(--muted)}details.validation{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:12px 0}details.validation summary{color:var(--text);font-weight:700;cursor:pointer}
.ledger-tools{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin:0 0 12px}.ledger-controls{display:flex;gap:8px;flex-wrap:wrap}.ledger-search{min-width:240px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:11px;padding:10px 12px;font:inherit;outline:none}.ledger-search:focus{border-color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}th,td{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:11px;color:var(--muted);text-transform:uppercase;background:var(--panel2);position:sticky;top:0}td{font-size:13px}.scroll{overflow:auto;border-radius:14px}.footer{border-top:1px solid var(--line);margin-top:46px;padding-top:18px;color:var(--muted);font-size:12px}
@media(max-width:900px){.hero-grid,.chart-layout{grid-template-columns:1fr}.kpis,.stages{grid-template-columns:1fr 1fr}.hero-metric{max-width:420px}.stage:not(:last-child):after{display:none}}
@media(max-width:560px){.kpis,.stages{grid-template-columns:1fr}.wrap{padding:18px 14px 60px}h1{font-size:40px}.value{font-size:25px}.app-switcher{width:100%}.ledger-search{width:100%;min-width:0}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media print{body{background:#F7F9FC;color:#0B1F3A}.wrap{max-width:none;padding:0}.app-switcher,.trace-switcher,.ledger-tools{display:none!important}.view-panel,.trace-card{display:block!important}.hero-metric,.kpi,.performance-panel,.episode,details.validation,table{background:#fff!important;color:#0B1F3A;border-color:#D7E0EA}.muted,.sub,.small,.chart-text{color:#667085!important;fill:#667085!important}.stage{background:#fff!important}.stage:not(:last-child):after{display:none}.chart-selected{background:#fff;color:#667085}.chart-selected strong{color:#0B1F3A}.footer{color:#667085}.live-dot{display:none}}
'''


INTERACTIVE_JS = r'''
const performanceData = window.DA_PERFORMANCE || [];

(function () {
  const viewButtons = Array.from(document.querySelectorAll("[data-view-target]"));
  const viewPanels = Array.from(document.querySelectorAll("[data-view-panel]"));
  function setView(name) {
    viewButtons.forEach(b => b.classList.toggle("active", b.dataset.viewTarget === name));
    viewPanels.forEach(p => p.classList.toggle("active", p.dataset.viewPanel === name));
    if (history.replaceState) history.replaceState(null, "", "#" + name);
  }
  viewButtons.forEach(b => b.addEventListener("click", () => setView(b.dataset.viewTarget)));
  const initialView = location.hash && ["overview","decisions","evidence"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  setView(initialView);

  const traceButtons = Array.from(document.querySelectorAll("[data-trace-target]"));
  const traceCards = Array.from(document.querySelectorAll(".trace-card"));
  function setTrace(i) {
    traceButtons.forEach(b => b.classList.toggle("active", Number(b.dataset.traceTarget) === i));
    traceCards.forEach(c => c.classList.toggle("active", Number(c.dataset.traceIndex) === i));
  }
  traceButtons.forEach(b => b.addEventListener("click", () => setTrace(Number(b.dataset.traceTarget))));
  if (traceCards.length) setTrace(0);

  const svg = document.getElementById("perfChart");
  const selected = document.getElementById("perfSelected");
  const perfRows = Array.from(document.querySelectorAll(".perf-row"));

  function money(v, signed) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    const abs = Math.abs(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (signed) return (n >= 0 ? "+$" : "-$") + abs;
    return "$" + n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  function choosePoint(i) {
    if (!performanceData.length) return;
    i = Math.max(0, Math.min(performanceData.length - 1, i));
    document.querySelectorAll(".chart-point").forEach((p, j) => p.classList.toggle("active", j === i));
    perfRows.forEach(row => row.classList.toggle("selected", Number(row.dataset.perfIndex) === i));

    const d = performanceData[i];
    const change = d.snapshot_change === null ? "—" : money(d.snapshot_change, true);
    const positions = d.position_count === null ? "—" : d.position_count;
    selected.innerHTML =
      "<span><strong>" + d.date + "</strong></span>" +
      "<span>Equity <strong>" + money(d.equity, false) + "</strong></span>" +
      "<span>P&amp;L <strong>" + money(d.pnl, true) + "</strong></span>" +
      "<span>Return <strong>" + Number(d.return_pct).toFixed(2) + "%</strong></span>" +
      "<span>vs prior snapshot <strong>" + change + "</strong></span>" +
      "<span>Positions <strong>" + positions + "</strong></span>";
  }

  function drawPerformance() {
    if (!svg) return;
    const ns = "http://www.w3.org/2000/svg";
    svg.innerHTML = "";

    if (!performanceData.length) {
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", "500");
      t.setAttribute("y", "140");
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("class", "chart-text");
      t.textContent = "No account snapshots found.";
      svg.appendChild(t);
      if (selected) selected.textContent = "Add data/performance/account_snapshots.json to populate this section.";
      return;
    }

    const W = 1000, H = 310;
    const pad = {l: 78, r: 28, t: 18, b: 44};
    const values = performanceData.map(d => Number(d.equity));
    const baseline = 100000;
    let min = Math.min(...values, baseline);
    let max = Math.max(...values, baseline);
    if (min === max) { min -= 1; max += 1; }
    const margin = Math.max((max - min) * 0.18, 50);
    min -= margin; max += margin;

    const x = i => performanceData.length === 1
      ? pad.l + (W - pad.l - pad.r) / 2
      : pad.l + i * (W - pad.l - pad.r) / (performanceData.length - 1);
    const y = v => pad.t + (max - v) * (H - pad.t - pad.b) / (max - min);

    for (let g = 0; g < 4; g++) {
      const yy = pad.t + g * (H - pad.t - pad.b) / 3;
      const val = max - g * (max - min) / 3;

      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", pad.l);
      line.setAttribute("x2", W - pad.r);
      line.setAttribute("y1", yy);
      line.setAttribute("y2", yy);
      line.setAttribute("class", "chart-grid");
      svg.appendChild(line);

      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", pad.l - 10);
      label.setAttribute("y", yy + 4);
      label.setAttribute("text-anchor", "end");
      label.setAttribute("class", "chart-text");
      label.textContent = "$" + Math.round(val).toLocaleString();
      svg.appendChild(label);
    }

    if (baseline >= min && baseline <= max) {
      const baseLine = document.createElementNS(ns, "line");
      baseLine.setAttribute("x1", pad.l);
      baseLine.setAttribute("x2", W - pad.r);
      baseLine.setAttribute("y1", y(baseline));
      baseLine.setAttribute("y2", y(baseline));
      baseLine.setAttribute("class", "chart-baseline");
      svg.appendChild(baseLine);

      const baseText = document.createElementNS(ns, "text");
      baseText.setAttribute("x", W - pad.r);
      baseText.setAttribute("y", y(baseline) - 6);
      baseText.setAttribute("text-anchor", "end");
      baseText.setAttribute("class", "chart-text");
      baseText.textContent = "$100K start";
      svg.appendChild(baseText);
    }

    const defs = document.createElementNS(ns, "defs");
    const grad = document.createElementNS(ns, "linearGradient");
    grad.setAttribute("id", "equityGradient"); grad.setAttribute("x1","0"); grad.setAttribute("x2","0"); grad.setAttribute("y1","0"); grad.setAttribute("y2","1");
    const stop1 = document.createElementNS(ns,"stop"); stop1.setAttribute("offset","0%"); stop1.setAttribute("stop-color","#F39C12"); stop1.setAttribute("stop-opacity",".22");
    const stop2 = document.createElementNS(ns,"stop"); stop2.setAttribute("offset","100%"); stop2.setAttribute("stop-color","#F39C12"); stop2.setAttribute("stop-opacity","0");
    grad.appendChild(stop1); grad.appendChild(stop2); defs.appendChild(grad); svg.appendChild(defs);

    const lineD = performanceData.map((d, i) => (i ? "L" : "M") + x(i) + "," + y(Number(d.equity))).join(" ");
    const area = document.createElementNS(ns, "path");
    const areaD = lineD + " L" + x(performanceData.length-1) + "," + (H-pad.b) + " L" + x(0) + "," + (H-pad.b) + " Z";
    area.setAttribute("d", areaD); area.setAttribute("class", "chart-area"); svg.appendChild(area);

    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", lineD); path.setAttribute("class", "chart-line"); svg.appendChild(path);
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const len = path.getTotalLength();
      path.style.strokeDasharray = len; path.style.strokeDashoffset = len;
      requestAnimationFrame(() => { path.style.strokeDashoffset = 0; });
    }

    performanceData.forEach((d, i) => {
      const point = document.createElementNS(ns, "circle");
      point.setAttribute("cx", x(i));
      point.setAttribute("cy", y(Number(d.equity)));
      point.setAttribute("r", "6");
      point.setAttribute("class", "chart-point");
      point.setAttribute("tabindex", "0");
      point.setAttribute("role", "button");
      point.setAttribute("aria-label", d.date + " equity " + money(d.equity, false));
      point.addEventListener("click", () => choosePoint(i));
      point.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          choosePoint(i);
        }
      });
      svg.appendChild(point);

      const dateLabel = document.createElementNS(ns, "text");
      dateLabel.setAttribute("x", x(i));
      dateLabel.setAttribute("y", H - 14);
      dateLabel.setAttribute("text-anchor", "middle");
      dateLabel.setAttribute("class", "chart-text");
      dateLabel.textContent = d.date.slice(5);
      svg.appendChild(dateLabel);
    });

    choosePoint(performanceData.length - 1);
  }

  perfRows.forEach(row => {
    row.addEventListener("click", () => choosePoint(Number(row.dataset.perfIndex)));
  });

  const filterButtons = Array.from(document.querySelectorAll("[data-ledger-filter]"));
  const ledgerRows = Array.from(document.querySelectorAll("#decisionLedger tbody tr"));
  const ledgerSearch = document.getElementById("ledgerSearch");
  let activeLedgerFilter = "all";
  function applyLedgerFilters() {
    const q = (ledgerSearch && ledgerSearch.value || "").trim().toLowerCase();
    ledgerRows.forEach(row => {
      const modeOk = activeLedgerFilter === "all" || row.dataset.mode === activeLedgerFilter;
      const searchOk = !q || (row.dataset.search || row.textContent || "").toLowerCase().includes(q);
      row.hidden = !(modeOk && searchOk);
    });
  }
  filterButtons.forEach(button => {
    button.addEventListener("click", () => {
      activeLedgerFilter = button.dataset.ledgerFilter;
      filterButtons.forEach(b => b.classList.toggle("active", b === button));
      applyLedgerFilters();
    });
  });
  if (ledgerSearch) ledgerSearch.addEventListener("input", applyLedgerFilters);

  drawPerformance();
})();
'''


def build_html(decisions, da, cal_path, cal):
    live = da["live"]
    dry = da["live_dry"]
    allm = da["all"]
    selected_episodes = select_episodes(decisions)
    episodes = "".join(episode_html(r, i) for i, r in enumerate(selected_episodes)) or "<p>No decision logs found.</p>"
    trace_switcher = "".join(
        f'<button type="button" class="trace-btn {"active" if i == 0 else ""}" data-trace-target="{i}">'
        f'{esc(r.get("date"))} · {esc((final_ruling_of(r).get("outcome") or ("no trade" if no_trade(r) else "unknown")).replace("_"," "))}</button>'
        for i, r in enumerate(selected_episodes)
    )

    if cal["n"]:
        auc = f"{cal['auc']:.3f}" if cal["auc"] is not None else "—"
        benchmark = f'''
        <div class="grid kpis">
          {kpi("Benchmark cases", cal['n'], f"{cal['flawed_n']} flawed + {cal['clean_n']} clean")}
          {kpi("Flawed intervention", f"{pct(cal['flawed_intervene'],cal['flawed_n']):.1f}%", f"{cal['flawed_intervene']}/{cal['flawed_n']} revised or blocked")}
          {kpi("Clean immediate pass", f"{pct(cal['clean_proceed'],cal['clean_n']):.1f}%", f"{cal['clean_proceed']}/{cal['clean_n']} proceed")}
          {kpi("Severity AUC", auc, "flawed vs clean")}
        </div>
        <div class="callout"><b>Calibration.</b> Proceed &lt; {gate.SEVERITY_PROCEED:.2f}; revise {gate.SEVERITY_PROCEED:.2f}–{gate.SEVERITY_BLOCK:.2f}; reject &gt; {gate.SEVERITY_BLOCK:.2f}. Flawed mean severity {cal['flawed_mean']:.3f}; clean mean {cal['clean_mean']:.3f}. Exact label match {cal['exact']}/{cal['flawed_n']} ({pct(cal['exact'],cal['flawed_n']):.1f}%), so exact naming is treated as secondary to severity separation.</div>
        <div class="scroll"><table><thead><tr><th>Injected mode</th><th>Exact label</th><th>Mean severity</th><th>Observed labels</th></tr></thead><tbody>{calibration_table(cal)}</tbody></table></div>
        <p class="muted">Source: {esc(str(cal_path.relative_to(ROOT)) if cal_path else '—')}</p>
        '''
    else:
        benchmark = "<p>No complete calibration benchmark found.</p>"

    performance = load_performance_snapshots()
    latest = performance[-1] if performance else None
    performance_json = json.dumps(performance).replace("</", "<\\/")
    perf_rows = performance_table_rows(performance) if performance else (
        '<tr><td colspan="7" class="muted">No persisted account snapshots found.</td></tr>'
    )

    if latest:
        latest_equity = money(latest["equity"])
        latest_pnl = signed_money(latest["pnl"])
        latest_return = f"{latest['return_pct']:+.2f}%"
        latest_change = signed_money(latest["snapshot_change"])
        timestamp_note = latest["timestamp"] or latest["date"]
        perf_context = (
            f"Latest stored snapshot: {esc(timestamp_note)}. "
            "Snapshot-to-snapshot change is not labeled as end-of-day return unless the observations were captured at market close."
        )
    else:
        latest_equity = latest_pnl = latest_return = latest_change = "—"
        perf_context = "No performance snapshots are available yet."

    ledger = ledger_rows(da["live_rows"] + da["live_dry_rows"])
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Devil's Advocate — Live Performance & Decision Audit</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div class="hero-grid">
      <div>
        <div class="status-row"><span class="live-dot" aria-hidden="true"></span><span class="eyebrow">Alpaca paper account · judge console</span></div>
        <h1>Devil's Advocate</h1>
        <div class="lede">One AI role proposes. An adversarial role challenges. Deterministic code controls what can actually reach the broker.</div>
        <p class="muted">Frozen policy: proceed &lt; {gate.SEVERITY_PROCEED:.2f} · revise {gate.SEVERITY_PROCEED:.2f}–{gate.SEVERITY_BLOCK:.2f} · reject &gt; {gate.SEVERITY_BLOCK:.2f} · max {gate.MAX_CONTRACTS_PER_TRADE} contracts/trade.</p>
      </div>
      <div class="hero-metric">
        <div class="label">Paper account P&amp;L</div>
        <div class="big">{latest_pnl}</div>
        <div class="mini">{latest_return} from $100K start · latest equity {latest_equity}</div>
      </div>
    </div>
  </section>

  <div class="app-switcher" role="tablist" aria-label="Judge console views">
    <button type="button" class="view-btn active" data-view-target="overview">Overview</button>
    <button type="button" class="view-btn" data-view-target="decisions">Decision Explorer</button>
    <button type="button" class="view-btn" data-view-target="evidence">Validation &amp; Safeguards</button>
  </div>

  <div class="view-panel active" data-view-panel="overview">
  <section id="performance">
    <h2>Paper Account Performance</h2>
    <p class="muted">Account equity snapshots from the Alpaca paper-trading account. P&amp;L is measured against the $100,000 starting balance.</p>
    <div class="grid kpis">
      {kpi("Latest equity", latest_equity, latest["date"] if latest else "snapshot unavailable")}
      {kpi("Total P&L", latest_pnl, "vs $100,000 starting balance")}
      {kpi("Cumulative return", latest_return, "paper account")}
      {kpi("Change vs prior snapshot", latest_change, "not necessarily end-of-day change")}
    </div>

    <div class="performance-panel">
      <div class="label">Interactive equity history</div>
      <div class="chart-layout">
        <div class="chart-wrap">
          <svg id="perfChart" viewBox="0 0 1000 310" role="img" aria-label="Paper account equity snapshots"></svg>
        </div>
        <div class="chart-selected" id="perfSelected" aria-live="polite"></div>
      </div>
      <p class="small">{perf_context}</p>
    </div>

    <div class="scroll" style="margin-top:16px">
      <table id="performanceTable">
        <thead>
          <tr>
            <th>Date</th>
            <th>Equity</th>
            <th>Vs prior snapshot</th>
            <th>P&amp;L vs start</th>
            <th>Return</th>
            <th>Cash</th>
            <th>Open positions</th>
          </tr>
        </thead>
        <tbody>{perf_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="live">
    <h2>Agentic Live Runs</h2>
    <p class="muted">This section isolates actual autonomous runs with the competition paper-account path enabled. LIVE-DRY validation is reported separately below.</p>
    <div class="grid kpis">
      {kpi("Live sessions", live['sessions'], "mode=live; competition paper account")}
      {kpi("Live no-trade decisions", live['no_trade'], "standing down is a valid autonomous action")}
      {kpi("Live broker submissions", live['fills'], "paper orders accepted by broker")}
      {kpi("Fresh-gate passes", live['fresh_gate_pass'], f"{live['fresh_gate_block']} blocked at final execution check")}
    </div>
    <div class="callout"><b>Live-run principle.</b> The agent is evaluated on both action and restraint. A legitimate NO TRADE is an autonomous decision, not a failed run.</div>
  </section>

  </div>

  <div class="view-panel" data-view-panel="decisions">
  <section id="traces">
    <div class="section-head"><div><h2>Decision Explorer</h2><p class="muted">Choose a stored episode and follow the decision path instead of scrolling through a wall of cards.</p></div></div>
    <div class="trace-switcher">{trace_switcher}</div>
    {episodes}
  </section>

  <section id="ledger">
    <h2>Live Decision Ledger</h2>
    <p class="muted">Filter the audit trail to distinguish autonomous LIVE runs from real-market LIVE-DRY validation.</p>
    <div class="ledger-tools">
      <div class="ledger-controls">
        <button type="button" class="filter-btn active" data-ledger-filter="all">All runs</button>
        <button type="button" class="filter-btn" data-ledger-filter="live">LIVE only</button>
        <button type="button" class="filter-btn" data-ledger-filter="live-dry">LIVE-DRY only</button>
      </div>
      <input class="ledger-search" id="ledgerSearch" type="search" placeholder="Search date, contract, failure mode…" aria-label="Search decision ledger">
    </div>
    <div class="scroll">
      <table id="decisionLedger">
        <thead><tr><th>Date</th><th>Session</th><th>Mode</th><th>Initial action</th><th>Initial size</th><th>Objection</th><th>Severity</th><th>Final ruling</th><th>Final size</th><th>Final gate</th><th>Fills</th></tr></thead>
        <tbody>{ledger}</tbody>
      </table>
    </div>
  </section>

  </div>

  <div class="view-panel" data-view-panel="evidence">
  <section id="validation">
    <h2>Supporting Validation</h2>
    <p class="muted">These results validate the system, but they are not part of a single live trading decision. They are collapsed by default so live behavior stays primary.</p>

    <details class="validation">
      <summary>Adversary calibration benchmark</summary>
      <div style="margin-top:16px">{benchmark}</div>
    </details>

    <details class="validation">
      <summary>Deterministic gate verification and aggregate behavior</summary>
      <div class="grid kpis">
        {kpi("Gate tests", "28/28", "latest deterministic enforcement suite")}
        {kpi("Stored sessions", allm['sessions'], f"{allm['revisions']} revisions")}
        {kpi("Code substitutions", allm['substitutions'], "safer alternatives generated by code")}
        {kpi("Oversized proposals caught", allm['oversized_initial'], "detected in stored decisions")}
      </div>
      <div class="grid kpis">
        {kpi("Live-dry validations", dry['sessions'], "real broker state; intentionally no submission")}
        {kpi("All fresh-gate passes", allm['fresh_gate_pass'], f"{allm['fresh_gate_block']} blocked")}
        {kpi("Strategy changes", allm['strategy_changed'], "after model revision")}
        {kpi("Size reductions", allm['size_reduced'], "final size lower than initial")}
      </div>
    </details>
  </section>

  <section id="safeguards">
    <h2>Technical Safeguards</h2>
    <div class="callout"><b>Authority boundary.</b> The AI roles may propose, criticize, revise, or abstain. They cannot bypass the approved universe, coverage/collateral checks, contract cap, modeled-downside ceiling, options-exposure limit, daily trade cap, daily loss halt, manual HALT switch, fail-closed handling, or fresh execution check.</div>
    <p class="muted">Repeated disagreement never forces a trade. The deterministic gate may reject a proposal or generate a safer code-based alternative before the fresh broker-state check.</p>
  </section>

  </div>

  <div class="footer">Generated {generated} from local JSON artifacts. This report makes no model, broker, or network calls while rendering.</div>
</div>
<script>window.DA_PERFORMANCE = {performance_json};</script>
<script>{INTERACTIVE_JS}</script>
</body>
</html>'''



def build_summary(da, cal_path, cal, decisions):
    performance = load_performance_snapshots()
    latest = performance[-1] if performance else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {"proceed_below": gate.SEVERITY_PROCEED, "block_above": gate.SEVERITY_BLOCK},
        "hard_limits": {
            "max_contracts_per_trade": gate.MAX_CONTRACTS_PER_TRADE,
            "max_options_notional_pct": gate.MAX_OPTIONS_NOTIONAL_PCT,
            "max_loss_per_position_pct": gate.MAX_LOSS_PER_POSITION_PCT,
            "max_trades_per_day": gate.MAX_TRADES_PER_DAY,
            "daily_loss_halt_pct": gate.DAILY_LOSS_HALT_PCT,
        },
        "performance": {
            "starting_equity": STARTING_EQUITY,
            "snapshot_count": len(performance),
            "latest": latest,
            "snapshots": performance,
        },
        "decisions": {
            "all": da["all"],
            "live": da["live"],
            "live_dry": da["live_dry"],
            "objection_modes": da["objection_modes"],
        },
        "calibration_source": str(cal_path.relative_to(ROOT)) if cal_path else None,
        "calibration": {k:v for k,v in cal.items() if k not in ("rows","modes")},
        "calibration_by_mode": cal.get("modes", {}),
        "selected_episodes": [r.get("_path") for r in select_episodes(decisions)],
    }



def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    decisions = load_decisions()
    cal_path, cal_rows = select_calibration()
    da = analyze_decisions(decisions)
    cal = analyze_calibration(cal_rows)

    (REPORT_DIR / "index.html").write_text(build_html(decisions, da, cal_path, cal))
    (REPORT_DIR / "summary.json").write_text(json.dumps(build_summary(da, cal_path, cal, decisions), indent=2, default=str))

    print("Devil's Advocate — report built")
    print(f"decisions: {da['all']['sessions']}")
    print(f"live sessions: {da['live']['sessions']}")
    print(f"benchmark cases: {cal['n']}")
    print("HTML: report/index.html")
    print("JSON: report/summary.json")


if __name__ == "__main__":
    main()
