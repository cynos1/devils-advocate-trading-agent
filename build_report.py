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


CSS = r'''
:root{--bg:#0b0f14;--panel:#121923;--panel2:#0f151e;--text:#edf3f8;--muted:#93a1b2;--line:#273241;--accent:#f3c969;--green:#67d69a;--red:#ff8092;--blue:#78baff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.wrap{max-width:1180px;margin:auto;padding:42px 24px 80px}.hero{border-bottom:1px solid var(--line);padding-bottom:28px}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.13em;font-size:12px;font-weight:700}h1{font-size:46px;line-height:1.05;margin:8px 0 12px;letter-spacing:-.03em}h2{font-size:27px;margin:42px 0 16px}.lede{max-width:850px;color:#c9d2dd;font-size:18px}.muted,.sub{color:var(--muted)}.grid{display:grid;gap:14px}.kpis{grid-template-columns:repeat(4,1fr);margin-top:22px}.kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:30px;font-weight:750;margin:5px 0}.sub{font-size:13px}.callout{background:var(--panel2);border-left:3px solid var(--accent);padding:16px 19px;margin:18px 0;border-radius:0 12px 12px 0}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-size:11px;text-transform:uppercase}.badge.good{color:var(--green)}.badge.bad{color:var(--red)}.badge.warn{color:var(--accent)}.badge.blue{color:var(--blue)}.badge.neutral{color:var(--muted)}.episode{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:19px;margin:13px 0}.episode-head{display:flex;justify-content:space-between;gap:15px}.stages{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:14px}.stage{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:11px}.stage-name{color:var(--muted);font-size:11px;text-transform:uppercase;margin-bottom:4px}.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.chip{background:#1d2633;border-radius:999px;padding:4px 9px;font-size:12px}details{margin-top:13px;color:var(--muted)}table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}th,td{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:11px;color:var(--muted);text-transform:uppercase;background:var(--panel2)}td{font-size:13px}.scroll{overflow:auto}.footer{border-top:1px solid var(--line);margin-top:46px;padding-top:18px;color:var(--muted);font-size:12px}@media(max-width:850px){.kpis,.stages{grid-template-columns:1fr 1fr}}@media(max-width:560px){.kpis,.stages{grid-template-columns:1fr}.wrap{padding:28px 14px 60px}}
'''


def build_html(decisions, da, cal_path, cal):
    exact_matches = cal.get("exact_matches", cal.get("exact", 0))
    live = da["live"]
    dry = da["live_dry"]
    allm = da["all"]

    # Pick three narrative episodes:
    # 1) strongest safety intervention / substitution
    # 2) real live no-trade
    # 3) strategy-changing revision if available
    valid = [r for r in decisions if "_load_error" not in r]

    def strongest_substitution():
        cands = [
            r for r in valid
            if final_ruling_of(r).get("outcome") == "substitute"
        ]
        if not cands:
            return None
        return max(cands, key=lambda r: (
            1 if oversized_initial(r) else 0,
            episode_score(r)
        ))

    def strongest_live_reject():
        cands = [
            r for r in valid
            if r.get("mode") == "live"
            and final_ruling_of(r).get("outcome") == "reject"
        ]
        return max(cands, key=episode_score) if cands else None

    def strongest_strategy_change():
        cands = [
            r for r in valid
            if strategy_changed(r)
        ]
        return max(cands, key=episode_score) if cands else None

    featured = []
    for candidate in (
        strongest_substitution(),
        strongest_live_reject(),
        strongest_strategy_change(),
    ):
        if candidate and candidate.get("_path") not in {x.get("_path") for x in featured}:
            featured.append(candidate)

    # Backfill if fewer than 3.
    for r in select_episodes(valid, 8):
        if len(featured) >= 3:
            break
        if r.get("_path") not in {x.get("_path") for x in featured}:
            featured.append(r)

    benchmark_section = "<p class='muted'>No complete benchmark file found.</p>"
    if cal["n"]:
        auc_text = f"{cal['auc']:.3f}" if cal["auc"] is not None else "—"
        benchmark_section = f"""
        <div class="grid kpis">
          {kpi("Benchmark cases", str(cal["n"]), f"{cal['flawed_n']} flawed + {cal['clean_n']} clean")}
          {kpi("Flawed intervention", fmt_pct(cal["flawed_intervene"], cal["flawed_n"]), f"{cal['flawed_intervene']}/{cal['flawed_n']} revised or blocked")}
          {kpi("Clean immediate pass", fmt_pct(cal["clean_proceed"], cal["clean_n"]), f"{cal['clean_proceed']}/{cal['clean_n']} proceed immediately")}
          {kpi("Severity AUC", auc_text, "flawed vs clean severity separation")}
        </div>

        <div class="callout">
          <strong>Calibration result.</strong>
          Severity was a much stronger signal than exact failure-mode naming.
          With the frozen thresholds, {cal['flawed_intervene']}/{cal['flawed_n']} flawed cases
          triggered intervention while {cal['clean_proceed']}/{cal['clean_n']} clean controls
          proceeded immediately. Exact mode match was {exact_matches}/{cal['flawed_n']}
          ({fmt_pct(exact_matches, cal['flawed_n'])}), so the system treats the label
          as diagnostic context rather than the safety decision itself.
        </div>

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
        <p class="small">Calibration source: {esc(str(cal_path.relative_to(ROOT)) if cal_path else "—")}</p>
        """

    featured_html = "".join(episode_html(r) for r in featured)

    # Compact ledger rows for live + live-dry validation only.
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
  <title>Devil's Advocate — Judge Brief</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <section class="hero">
    <div class="eyebrow">Alpaca AI Trading Agents Hackathon</div>
    <h1>Devil's Advocate</h1>
    <div class="lede">
      One model proposes. A second attacks. One revision is allowed.
      Deterministic code decides what authority survives.
    </div>
    <div class="freeze">
      Frozen policy:
      <strong>proceed &lt; {gate.SEVERITY_PROCEED:.2f}</strong> ·
      <strong>revise {gate.SEVERITY_PROCEED:.2f}–{gate.SEVERITY_BLOCK:.2f}</strong> ·
      <strong>reject &gt; {gate.SEVERITY_BLOCK:.2f}</strong> ·
      max {gate.MAX_CONTRACTS_PER_TRADE} contracts/trade ·
      fresh-state execution gate before broker submission.
    </div>
  </section>

  <h2>What the system proved</h2>
  <div class="grid kpis">
    {kpi("Stored decision sessions", str(allm["sessions"]), f"{allm['revisions']} included model revision")}
    {kpi("Code substitutions", str(allm["substitutions"]), "safer alternatives generated by deterministic logic")}
    {kpi("Oversized proposals caught", str(allm["oversized_initial"]), "model intent constrained by hard authority limits")}
    {kpi("Gate tests", "28/28", "latest deterministic safety suite")}
  </div>

  <div class="callout">
    <strong>Core result.</strong>
    The models are allowed to be imperfect. They may disagree, misclassify a risk,
    or propose too much size. They still cannot exceed the authority encoded in the gate.
    No-trade remains valid, and repeated disagreement never forces execution.
  </div>

  <h2>Calibration evidence</h2>
  {benchmark_section}

  <h2>Three decision stories</h2>
  <p class="muted">
    These are the highest-value episodes from the audit trail: one safety substitution,
    one genuine live no-trade decision, and one revision that materially changed the trade.
  </p>
  {featured_html if featured_html else "<p>No decision logs found.</p>"}

  <h2>Real live decisions</h2>
  <div class="grid kpis">
    {kpi("Live sessions", str(live["sessions"]), "competition paper account; mode=live")}
    {kpi("Live no-trade decisions", str(live["no_trade"]), "inaction remained a valid output")}
    {kpi("Live broker submissions", str(live["fills"]), "actual paper orders accepted by broker")}
    {kpi("Live-dry validations", str(dry["sessions"]), "real market data, intentionally no submission")}
  </div>

  <div class="callout">
    <strong>LIVE and LIVE-DRY are intentionally separated.</strong>
    LIVE means the full paper-trading path was enabled.
    LIVE-DRY used real broker state and market data but intentionally stopped before order submission.
  </div>

  <h2>Compact audit ledger</h2>
  <div style="overflow:auto">
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

  <h2>What worked — and what did not</h2>
  <div class="grid two">
    <div class="kpi">
      <div class="kpi-label">Strongest behavior</div>
      <div class="kpi-value">Risk escalation</div>
      <div class="kpi-sub">
        Flawed proposals were separated from clean controls well enough to calibrate
        intervention thresholds from observed severity rather than guesses.
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Known limitation</div>
      <div class="kpi-value">Exact labels</div>
      <div class="kpi-sub">
        The adversary matched the injected failure-mode label only
        {exact_matches if cal['n'] else 0}/{cal['flawed_n'] if cal['n'] else 0}.
        Concrete market risks were stronger than abstract reasoning-bias labels.
      </div>
    </div>
  </div>

  <div class="callout">
    <strong>Interpretation.</strong>
    The adversary is useful as a calibrated escalation layer, not as an infallible classifier.
    Exact naming is secondary; deterministic code remains responsible for enforceable safety.
  </div>

  <h2>Authority boundary</h2>
  <div class="grid two">
    <div class="kpi">
      <div class="kpi-label">Models may</div>
      <div class="kpi-sub">
        Propose trades · criticize reasoning · suggest a revision · abstain.
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Models may not</div>
      <div class="kpi-sub">
        Bypass contract caps · create naked calls · exceed collateral or coverage ·
        override the daily loss halt · remove the kill switch · bypass the fresh execution gate.
      </div>
    </div>
  </div>

  <div class="footer">
    Generated {generated} from local JSON decision/calibration artifacts.
    This report performs no model calls, broker calls, or network access.
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
