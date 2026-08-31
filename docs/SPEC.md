# Devil's Advocate — Build Specification

**Alpaca AI Trading Agents Hackathon, 28 Aug – 4 Sep 2026**
**Competition account:** PA31ZW6SDLLO
**Holdings as of 25 Aug:** 400 XLE, 400 XLF, $51,696 cash, options level 3
**Revised:** 25 August 2026 — liquidity rules rewritten against real chain data

An options agent in which one model proposes a trade, a second model attacks
the reasoning, the proposer gets exactly one revision, and deterministic code
makes the final call. No model can override a safety constraint.

---

## 1. The pipeline

```
market data  →  PROPOSER  →  ADVERSARY  →  one revision  →  ADVERSARY
                                                                 ↓
   JSON journal  ←  execution  ←  RISK GATE (deterministic code)
        ↓
   static HTML report
```

**Exactly one revision.** No further negotiation. If the revised proposal
still fails the gate, the gate may substitute a safer alternative **generated
by code** — fewer contracts, or the next contract on a prefiltered approved
list. No third model call. Ever.

---

## 2. Approved strategies

The proposer may only choose from these three.

### Covered call
Sell a call against shares already owned. Collect premium; cap upside above
the strike.
- Requires: 100 owned shares per contract
- Max loss: the underlying falling, softened by the premium
- Assignment: shares delivered at the strike

### Cash-secured put
Sell a put while holding enough cash to buy the shares if assigned.
- Requires: strike × 100 in cash reserved per contract
- Max loss: (strike × 100) − premium
- Assignment: shares purchased at the strike

### Protective put
Buy a put against shares owned. Pay premium; floor the downside.
- Requires: 100 owned shares per contract
- Max loss: premium paid, plus the drop to the strike
- Costs money rather than earning it — the proposer must justify it as a hedge

**All three carry real market exposure.** The proposer is not predicting
direction, but every position changes downside exposure and assignment
obligation. The journal states this explicitly for each trade.

---

## 3. Universe

| Symbol | Spot (25 Aug) | Held | Contracts available |
|---|---|---|---|
| XLE | $62.63 | 400 sh | 4 |
| XLF | $58.20 | 400 sh | 4 |

Two underlyings, deliberately. With one, the `concentration` failure mode
could never fire and one of the adversary's eight modes would be decorative.

Both were chosen from screened chain data: cheap enough that $100k buys
several round lots, liquid enough to pass the filters below. IWM and SPY were
rejected — one round lot costs $30k and $77k respectively, which would put
most of the account in a single position and leave the agent almost nothing
to decide.

---

## 4. The adversary's named failure modes

Vague instructions produce vague critique. The adversary hunts for these
eight specifically and names which one it found.

| Mode | What it means |
|---|---|
| `overconfidence` | Reasoning states more certainty than the evidence supports |
| `recency_bias` | Conclusion rests on the last few sessions of price action |
| `insufficient_premium` | Income does not compensate for the risk assumed |
| `poor_liquidity` | Thin open interest, stale last trade, or wide bid-ask |
| `concentration` | Too much tied to one underlying or one expiry |
| `assignment_risk` | Strike too near the money given time remaining |
| `unfavourable_expiry` | Expiry too close, too far, or spanning a known event |
| `position_conflict` | Contradicts or duplicates an existing open position |

The mode is a structured field, not just prose. At the end you can report
which objections fired most often — that is a finding, not just a log.

---

## 5. The risk gate — hard limits

Plain Python. Checked on every trade, after both models have had their say.

### Position and exposure

| Limit | Value |
|---|---|
| Approved underlyings | XLE, XLF only |
| Contracts per trade | ≤ 3 |
| Open contracts per underlying | ≤ 4 (equals lots held) |
| Total options notional | ≤ 40% of equity |
| Max loss per position | ≤ 5% of equity |
| Covered call coverage | shares owned ÷ 100. Never naked |
| Cash-secured put collateral | strike × 100 × contracts must exist as cash |
| Trades per day | ≤ 4 |
| Daily loss halt | −3% of equity |
| Kill switch | `HALT` file present |

### Contract eligibility — rewritten from real data

The original spec filtered on bid-ask spread as a percentage of mid. **Real
chain data showed that rule is broken**, and the correction matters enough to
record here.

SPY's most heavily held contract on 25 August had 36,652 open interest — as
liquid as options get — and an 18.2% spread. But the mid was $0.06, so the
actual gap was about a penny. Percentages become meaningless on near-worthless
contracts. Meanwhile IWM's most-held contract showed a 133% spread on a $0.03
mid. Neither contract is illiquid. Both are simply almost worthless.

A percentage-only filter would therefore reject good contracts and accept
untradeable ones. Three rules are needed together:

| Limit | Value | Why |
|---|---|---|
| Contract mid | ≥ $0.20 | Below this, percentage spread is noise |
| Premium per contract | ≥ $25 | Not worth the transaction |
| Bid-ask spread | ≤ 10% of mid | Only meaningful once mid clears $0.20 |
| Open interest | ≥ 100 | Contracts nobody holds are hard to exit |
| Last trade age | ≤ 3 trading days | **A contract can be quoted and still be dead** |
| Expiry window | 3 to 21 days out | Resolves inside the judging window |

**On last trade age.** A live quote does not mean a live contract. On 25
August a SPY contract showed a current quote alongside a last trade from
17 August — eight days stale. Market makers quote continuously whether or not
anyone is trading. Requiring a recent print is the difference between a
contract that *can* be traded and one that merely *appears* tradeable.

This gives `poor_liquidity` a concrete definition rather than a vibe: it fires
on thin open interest, a stale last print, or a wide spread on a contract that
clears the mid floor.

**Every limit is enforced in code.** The prompts describe them so the models
propose sensibly, but the gate is what makes them true.

---

## 6. Structured outputs

### Proposer

```json
{
  "action": "covered_call" | "cash_secured_put" | "protective_put" | "no_trade",
  "underlying": "XLE",
  "contract_symbol": "XLE260904C00064000",
  "strike": 64.0,
  "expiry": "2026-09-04",
  "contracts": 1,
  "expected_premium": 142.00,
  "max_loss": 6263.00,
  "reasoning": "two or three sentences"
}
```

### Adversary

```json
{
  "severity": 0.0,
  "failure_mode": "assignment_risk",
  "objection": "two sentences naming the specific problem",
  "what_would_fix_it": "a concrete change — further strike, later expiry, fewer contracts"
}
```

`what_would_fix_it` is what makes the revision round productive rather than a
rerun.

### Parsing

Both go through an airlock. Malformed JSON, missing fields, unknown enum
values, or an unreachable API all resolve to **no trade**, logged with the
reason. The model never gets the benefit of the doubt.

`severity` is clamped to [0, 1] regardless of what comes back.

---

## 7. Severity thresholds — provisional, to be calibrated

| Severity | Resolution |
|---|---|
| < 0.4 | proceed as proposed |
| 0.4 – 0.7 | one revision, then re-assess |
| > 0.7 | reject; gate may substitute a code-generated safer alternative |

**These numbers are guesses and must be replaced.** Models cluster their
severity scores — typically 0.6 to 0.8 for almost everything — so thresholds
chosen by intuition may sit entirely inside or outside the cluster, making the
system either paralysed or permissive.

**Saturday 29 August:** run 30 mock proposals through the adversary, record
the distribution, and set thresholds at observed percentiles. Roughly: block
the top 15%, revise the next 35%, proceed with the rest.

The write-up states that the thresholds were set from measured data.

### Deadlock floor

If the adversary blocks three sessions running, escalate: log the streak
prominently and force the most conservative available trade or flag for
review. An agent that quietly does nothing forever is a failure that hides
itself.

---

## 8. Out of scope

- Multi-leg spreads
- Volatility modelling or forecasting
- Live interactive dashboard — static HTML generated from the JSON journal
- Any strategy requiring a directional view
- More than one revision round
- Any model involvement in the risk gate

---

## 9. What gets submitted

**One page:** AI logic, risk gates, Alpaca infrastructure — their headings,
their order.

**Static HTML report** from the JSON journal: proposed trade, objection and
its named mode, the revision, the final decision, max loss, assignment
obligation, account performance.

**The claim:** not that the agent trades well. That you can state exactly what
it will and will not do, regardless of what either model says.

---

## 10. Schedule

| Day | Work | Status |
|---|---|---|
| Tue 25 Aug | Spec. Account seeded. Universe chosen from real data. | done |
| Wed 26 Aug | Options data layer, contract filtering, mock broker. | |
| Thu 27 Aug | Prompts drafted. Test scenarios. Dry pipeline. | |
| **Fri 28 Aug** | **Kickoff 11am ET.** Full pipeline, one complete run. | |
| Sat 29 Aug | Risk engine hardening. Adversarial tests. **Calibration.** | |
| Sun 30 Aug | HTML report. Write-up draft. Scheduling. | |
| Mon 31 Aug | First full live session. **Checkpoint: fall back if needed.** | |
| Tue 1 Sep | Second live session. Best debate example selected. | |
| Wed 2 Sep | Light — Zoom demo. Confirm the run fired. | |
| Thu 3 Sep | Write-up finished. Report finalised. | |
| Fri 4 Sep | Final resolution. **Submit by midday.** | |
