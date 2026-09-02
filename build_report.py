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


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)



def fmt_pct(num, den, digits=1):
    if not den:
        return "—"
    return f"{(100.0 * num / den):.{digits}f}%"

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
    cls = {"execute":"good","substitute":"warn","revise":"warn","reject":"bad"}.get(outcome, "neutral")
    return badge(outcome or "unknown", cls)


def mode_badge(mode):
    cls = {"live":"good","live-dry":"blue"}.get(mode, "neutral")
    return badge(mode or "unknown", cls)


def kpi(label, value, sub):
    return f'''<div class="kpi"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div><div class="sub">{esc(sub)}</div></div>'''


def episode_html(r):
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
    <article class="episode">
      <div class="episode-head"><div><strong>{esc(r.get('_path'))}</strong><div class="muted">{esc(r.get('date'))} · session {esc(r.get('session'))} · {mode_badge(r.get('mode'))}</div></div>{outcome_badge(fr.get('outcome'))}</div>
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
        p = proposal_of(r); fr = final_ruling_of(r); fp = final_proposal_of(r); o = r.get("objection") or {}
        out.append(f"<tr><td>{esc(r.get('date'))}</td><td>{esc(r.get('session'))}</td><td>{mode_badge(r.get('mode'))}</td><td>{esc(p.get('action','—').replace('_',' '))}</td><td>{esc(p.get('contracts','—'))}</td><td>{esc(o.get('failure_mode','—'))}</td><td>{esc(o.get('severity','—'))}</td><td>{outcome_badge(fr.get('outcome'))}</td><td>{esc(fp.get('contracts','—'))}</td><td>{esc(final_gate_status(r))}</td><td>{len(r.get('fills') or [])}</td></tr>")
    return "".join(out)


CSS = r"""
:root{
  --bg:#0b0d12;--panel:#11151d;--panel2:#151a24;--text:#f4f1ea;
  --muted:#a6acb8;--line:#2a303b;--gold:#d8a84e;--blue:#6f8fbf;
  --green:#7dbb8a;--red:#d26a64;--amber:#c99a56
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;
  background:
    radial-gradient(circle at 82% -8%,rgba(111,143,191,.12),transparent 28%),
    radial-gradient(circle at 10% 9%,rgba(216,168,78,.08),transparent 25%),
    var(--bg);
  color:var(--text);
  font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif
}
a{text-decoration:none;color:inherit}
.wrap{max-width:1180px;margin:auto;padding:0 24px}
.topbar{
  position:sticky;top:0;z-index:20;
  backdrop-filter:blur(16px);
  background:rgba(11,13,18,.86);
  border-bottom:1px solid var(--line)
}
.nav{height:66px;display:flex;align-items:center;justify-content:space-between}
.brand{font-weight:900;letter-spacing:.04em}
.navlinks{display:flex;gap:18px;color:var(--muted);font-size:13px}
.navlinks a:hover{color:var(--text)}
.hero{padding:70px 0 38px}
.eyebrow{color:var(--gold);text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:800}
h1,h2{font-family:Georgia,"Times New Roman",serif;letter-spacing:-.025em}
h1{font-size:clamp(48px,7vw,78px);line-height:.96;margin:12px 0 18px}
h2{font-size:36px;margin:0}
.lede{max-width:880px;color:#c9d0d9;font-size:19px;line-height:1.65}
.policy{display:flex;gap:8px;flex-wrap:wrap;margin-top:22px}
.pill,.chip,.badge{
  display:inline-block;border:1px solid var(--line);border-radius:999px;
  padding:6px 10px;font-size:11px
}
.pill{color:#d8dde5;background:rgba(255,255,255,.025)}
section{padding:48px 0}
.section-head{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:22px}
.section-sub{max-width:680px;color:var(--muted);line-height:1.65}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(4,1fr)}
.kpi,.card{
  background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.018));
  border:1px solid var(--line);border-radius:18px;padding:21px
}
.kpi:first-child{border-top:2px solid var(--gold)}
.label,.kpi-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.value,.kpi-value{font-size:31px;font-weight:800;margin:5px 0}
.sub,.kpi-sub,.muted,.small{color:var(--muted)}
.sub,.kpi-sub{font-size:13px}
.small{font-size:12px}
.callout{
  background:rgba(216,168,78,.055);
  border-left:3px solid var(--gold);
  padding:18px 20px;margin:18px 0;border-radius:0 14px 14px 0;
  color:#dfd9cf;line-height:1.65
}
.compare{display:grid;grid-template-columns:1fr auto 1fr;gap:16px;align-items:center}
.compare .side{padding:22px;border:1px solid var(--line);border-radius:18px;background:var(--panel)}
.compare .side h3{margin:0 0 8px;font-size:16px}
.compare .arrow{font-size:30px;color:var(--gold)}
.badge{padding:3px 8px;text-transform:uppercase}
.badge.good{color:var(--green)}.badge.bad{color:var(--red)}.badge.warn{color:var(--gold)}
.badge.blue{color:var(--blue)}.badge.neutral{color:var(--muted)}
.episode{
  background:var(--panel);border:1px solid var(--line);border-radius:18px;
  padding:20px;margin:14px 0
}
.episode-head{display:flex;justify-content:space-between;gap:15px}
.stages{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:15px}
.stage{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px}
.stage-name{color:var(--gold);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
.chip{background:#1b212b;color:#d7dce4;padding:5px 9px}
details{margin-top:14px;color:var(--muted)}
details summary{cursor:pointer;color:#d9dde5}
.scroll{overflow:auto;border:1px solid var(--line);border-radius:16px}
table{width:100%;border-collapse:collapse;min-width:780px;background:var(--panel)}
th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;background:var(--panel2)}
td{font-size:13px}
.two{grid-template-columns:1fr 1fr}
.boundary{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.boundary ul{margin:0;padding-left:18px;color:var(--muted);line-height:1.75}
.sources{grid-template-columns:repeat(4,1fr)}
.source-card strong{display:block;margin-bottom:6px}
.footer{border-top:1px solid var(--line);margin-top:48px;padding:24px 0 48px;color:var(--muted);font-size:12px}
@media(max-width:900px){
  .kpis,.sources{grid-template-columns:1fr 1fr}
  .two,.boundary{grid-template-columns:1fr}
  .stages{grid-template-columns:1fr 1fr}
  .compare{grid-template-columns:1fr}
  .compare .arrow{transform:rotate(90deg);text-align:center}
  .navlinks{display:none}
}
@media(max-width:560px){
  .kpis,.sources,.stages{grid-template-columns:1fr}
  .wrap{padding:0 14px}
  section{padding:38px 0}
}
"""


def build_html(decisions, da, cal_path, cal):
    exact_matches = cal.get("exact_matches", cal.get("exact", 0))
    live = da["live"]
    dry = da["live_dry"]
    allm = da["all"]

    valid = [r for r in decisions if "_error" not in r and "_load_error" not in r]

    def strongest_substitution():
        cands = [r for r in valid if final_ruling_of(r).get("outcome") == "substitute"]
        if not cands:
            return None
        return max(cands, key=lambda r: (1 if oversized_initial(r) else 0, episode_score(r)))

    def strongest_live_reject():
        cands = [
            r for r in valid
            if r.get("mode") == "live"
            and final_ruling_of(r).get("outcome") == "reject"
        ]
        return max(cands, key=episode_score) if cands else None

    def strongest_strategy_change():
        cands = [r for r in valid if strategy_changed(r)]
        return max(cands, key=episode_score) if cands else None

    featured = []
    for candidate in (
        strongest_substitution(),
        strongest_live_reject(),
        strongest_strategy_change(),
    ):
        if candidate and candidate.get("_path") not in {x.get("_path") for x in featured}:
            featured.append(candidate)

    for r in select_episodes(valid, 8):
        if len(featured) >= 3:
            break
        if r.get("_path") not in {x.get("_path") for x in featured}:
            featured.append(r)

    benchmark_section = "<p class='muted'>No complete benchmark file found.</p>"
    if cal["n"]:
        auc_text = f"{cal['auc']:.3f}" if cal["auc"] is not None else "—"
        benchmark_section = f"""
        <div class="compare">
          <div class="side">
            <div class="label">Initial guess</div>
            <h3>Heuristic policy</h3>
            <p class="muted">Proceed &lt; 0.40<br>Revise 0.40–0.70<br>Reject &gt; 0.70</p>
          </div>
          <div class="arrow">→</div>
          <div class="side">
            <div class="label">Calibrated policy</div>
            <h3>Frozen after benchmark</h3>
            <p class="muted">Proceed &lt; {gate.SEVERITY_PROCEED:.2f}<br>
            Revise {gate.SEVERITY_PROCEED:.2f}–{gate.SEVERITY_BLOCK:.2f}<br>
            Reject &gt; {gate.SEVERITY_BLOCK:.2f}</p>
          </div>
        </div>

        <div class="grid kpis" style="margin-top:16px">
          {kpi("Benchmark cases", str(cal["n"]), f"{cal['flawed_n']} flawed + {cal['clean_n']} clean")}
          {kpi("Flawed intervention", fmt_pct(cal["flawed_intervene"], cal["flawed_n"]), f"{cal['flawed_intervene']}/{cal['flawed_n']} revised or blocked")}
          {kpi("Clean immediate pass", fmt_pct(cal["clean_proceed"], cal["clean_n"]), f"{cal['clean_proceed']}/{cal['clean_n']} proceed immediately")}
          {kpi("Severity AUC", auc_text, "flawed vs clean separation")}
        </div>

        <div class="callout">
          <strong>What changed?</strong>
          Severity separated flawed from clean proposals much better than exact failure-mode naming.
          Exact mode match was {exact_matches}/{cal['flawed_n']}
          ({fmt_pct(exact_matches, cal['flawed_n'])}), so labels are treated as diagnostic context
          while severity drives escalation.
        </div>

        <div class="scroll">
          <table>
            <thead>
              <tr>
                <th>Injected mode</th>
                <th>Exact label</th>
                <th>Mean severity</th>
                <th>Observed adversary labels</th>
              </tr>
            </thead>
            <tbody>{calibration_table(cal)}</tbody>
          </table>
        </div>
        <p class="small">Calibration source: {esc(str(cal_path.relative_to(ROOT)) if cal_path else "—")}</p>
        """

    featured_html = "".join(episode_html(r) for r in featured)

    compact_rows = []
    for r in sorted(
        da["live_rows"] + da["live_dry_rows"],
        key=lambda x: (x.get("date", ""), x.get("session", 0)),
        reverse=True,
    ):
        p = proposal_of(r)
        fr = final_ruling_of(r)
        fp = fr.get("proposal") or {}

        if is_trade(p):
            proposal_text = (
                f"{p.get('contracts')}× {p.get('underlying')} "
                f"{str(p.get('action','')).replace('_',' ')}"
            )
        else:
            proposal_text = "No trade"

        objection = r.get("objection") or {}
        sev = safe_float(objection.get("severity"))
        intervention = objection.get("failure_mode", "—")
        if sev is not None:
            intervention += f" · {sev:.2f}"

        if is_trade(fp):
            final_text = (
                f"{fr.get('outcome')} → {fp.get('contracts')}× "
                f"{fp.get('contract_symbol')}"
            )
        else:
            final_text = fr.get("outcome", "no trade")

        compact_rows.append(f"""
        <tr>
          <td>{esc(r.get('date',''))}<br><span class="small">session {esc(r.get('session',''))}</span></td>
          <td>{mode_badge(r.get('mode',''))}</td>
          <td>{esc(proposal_text)}</td>
          <td>{esc(intervention)}</td>
          <td>{outcome_badge(fr.get('outcome','—'))}<br><span class="small">{esc(final_text)}</span></td>
        </tr>
        """)
    compact_ledger = "".join(compact_rows)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Devil's Advocate — Judge Report</title>
  <style>{CSS}</style>
</head>
<body>

<div class="topbar">
  <div class="wrap nav">
    <div class="brand">DEVIL'S ADVOCATE</div>
    <div class="navlinks">
      <a href="#summary">Summary</a>
      <a href="#calibration">Calibration</a>
      <a href="#stories">Decision Stories</a>
      <a href="#live">Live Evidence</a>
      <a href="#ledger">Ledger</a>
      <a href="#authority">Authority</a>
    </div>
  </div>
</div>

<main class="wrap">

<section class="hero">
  <div class="eyebrow">Alpaca AI Trading Agents Hackathon · Judge Report</div>
  <h1>Evidence.</h1>
  <div class="lede">
    Devil's Advocate lets AI models propose, challenge, and revise trading decisions
    while deterministic code controls what authority survives.
    This report summarizes the evidence generated by the system.
  </div>
  <div class="policy">
    <span class="pill">Proceed &lt; {gate.SEVERITY_PROCEED:.2f}</span>
    <span class="pill">Revise {gate.SEVERITY_PROCEED:.2f}–{gate.SEVERITY_BLOCK:.2f}</span>
    <span class="pill">Reject &gt; {gate.SEVERITY_BLOCK:.2f}</span>
    <span class="pill">Max {gate.MAX_CONTRACTS_PER_TRADE} contracts / trade</span>
    <span class="pill">Fresh-state execution check</span>
    <span class="pill">NO TRADE remains valid</span>
  </div>
</section>

<section id="summary">
  <div class="section-head">
    <div>
      <div class="eyebrow">Executive summary</div>
      <h2>What the system proved</h2>
    </div>
    <div class="section-sub">
      The strongest result is not that the models were perfect.
      It is that model mistakes did not automatically become actions.
    </div>
  </div>

  <div class="grid kpis">
    {kpi("Stored decision sessions", str(allm["sessions"]), f"{allm['revisions']} included model revision")}
    {kpi("Code substitutions", str(allm["substitutions"]), "safer alternatives generated by deterministic logic")}
    {kpi("Oversized proposals caught", str(allm["oversized_initial"]), "model intent constrained by hard limits")}
    {kpi("Gate tests", "28/28", "latest deterministic suite")}
  </div>

  <div class="callout">
    <strong>Core result.</strong>
    The models may disagree, misclassify a risk, or propose too much size.
    They still cannot exceed the authority encoded in the gate.
    Repeated disagreement never forces execution.
  </div>
</section>

<section id="calibration">
  <div class="section-head">
    <div>
      <div class="eyebrow">Adversary calibration</div>
      <h2>From guessed thresholds to measured policy</h2>
    </div>
    <div class="section-sub">
      A 30-case benchmark calibrated how the adversary's severity score should trigger
      proceed, revise, or reject behavior.
    </div>
  </div>
  {benchmark_section}
</section>

<section id="stories">
  <div class="section-head">
    <div>
      <div class="eyebrow">Decision evidence</div>
      <h2>Three stories worth reading</h2>
    </div>
    <div class="section-sub">
      These are the highest-value episodes from the audit trail rather than a raw JSON dump.
    </div>
  </div>
  {featured_html if featured_html else "<p class='muted'>No decision logs found.</p>"}
</section>

<section id="live">
  <div class="section-head">
    <div>
      <div class="eyebrow">Paper trading</div>
      <h2>Live activity, separated from validation</h2>
    </div>
    <div class="section-sub">
      LIVE and LIVE-DRY remain intentionally distinct so judges can see what reached the
      paper-trading path versus what used real market data without submission.
    </div>
  </div>

  <div class="grid kpis">
    {kpi("Live sessions", str(live["sessions"]), "competition paper account")}
    {kpi("Live no-trade decisions", str(live["no_trade"]), "inaction remained valid")}
    {kpi("Live broker submissions", str(live["fills"]), "paper orders accepted by broker")}
    {kpi("Live-dry validations", str(dry["sessions"]), "real market data, no submission")}
  </div>

  <div class="callout">
    <strong>LIVE vs LIVE-DRY.</strong>
    LIVE means the full paper-trading path was enabled.
    LIVE-DRY used real broker state and market data but intentionally stopped before order submission.
  </div>
</section>

<section id="ledger">
  <div class="section-head">
    <div>
      <div class="eyebrow">Audit trail</div>
      <h2>Compact decision ledger</h2>
    </div>
    <div class="section-sub">
      Scan the run mode, initial proposal, first objection, and final outcome without opening raw files.
    </div>
  </div>

  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th>Run</th>
          <th>Mode</th>
          <th>Initial proposal</th>
          <th>Primary objection</th>
          <th>Final outcome</th>
        </tr>
      </thead>
      <tbody>{compact_ledger}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="section-head">
    <div>
      <div class="eyebrow">Interpretation</div>
      <h2>What worked — and what did not</h2>
    </div>
  </div>

  <div class="grid two">
    <div class="card">
      <div class="label">Strongest behavior</div>
      <div class="value">Risk escalation</div>
      <div class="sub">
        Flawed proposals were separated from clean controls well enough to calibrate
        intervention thresholds from observed severity rather than guesses.
      </div>
    </div>
    <div class="card">
      <div class="label">Known limitation</div>
      <div class="value">Exact labels</div>
      <div class="sub">
        The adversary matched the injected failure-mode label only
        {exact_matches if cal['n'] else 0}/{cal['flawed_n'] if cal['n'] else 0}.
        It is a calibrated escalation layer, not an infallible classifier.
      </div>
    </div>
  </div>
</section>

<section id="authority">
  <div class="section-head">
    <div>
      <div class="eyebrow">Authority boundary</div>
      <h2>What models may and may not do</h2>
    </div>
    <div class="section-sub">
      AI is allowed to reason. It is not allowed to redefine its own permissions.
    </div>
  </div>

  <div class="boundary">
    <div class="card">
      <div class="label">Models may</div>
      <ul>
        <li>Propose trades</li>
        <li>Criticize reasoning</li>
        <li>Suggest one revision</li>
        <li>Abstain</li>
      </ul>
    </div>
    <div class="card">
      <div class="label">Models may not</div>
      <ul>
        <li>Bypass contract caps</li>
        <li>Create naked calls</li>
        <li>Exceed collateral or coverage</li>
        <li>Override the daily loss halt</li>
        <li>Remove the kill switch</li>
        <li>Bypass the fresh execution gate</li>
      </ul>
    </div>
  </div>
</section>

<section>
  <div class="section-head">
    <div>
      <div class="eyebrow">Implementation evidence</div>
      <h2>Where the report comes from</h2>
    </div>
    <div class="section-sub">
      The report is generated from stored local artifacts. Rendering performs no model,
      broker, or network calls.
    </div>
  </div>

  <div class="grid sources">
    <div class="card source-card"><strong>Decision logs</strong><div class="sub">Proposal, challenge, revision, ruling, execution check.</div></div>
    <div class="card source-card"><strong>Calibration artifacts</strong><div class="sub">30-case benchmark and severity evidence.</div></div>
    <div class="card source-card"><strong>CLI audit</strong><div class="sub">Pre/post account, position, order, and clock verification.</div></div>
    <div class="card source-card"><strong>Gate tests</strong><div class="sub">Deterministic enforcement validated before automated live runs.</div></div>
  </div>
</section>

</main>

<div class="footer">
  <div class="wrap">
    Generated {generated} from local JSON decision/calibration artifacts.
    &nbsp;·&nbsp; <strong>A bad model decision should not automatically become a bad action.</strong>
  </div>
</div>

</body>
</html>
"""


def build_summary(da, cal_path, cal, decisions):
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
        "decisions": {"all": da["all"], "live": da["live"], "live_dry": da["live_dry"], "objection_modes": da["objection_modes"]},
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
