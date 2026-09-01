# Devil's Advocate

**An adversarial AI options-trading agent with bounded execution**

Devil's Advocate is an autonomous paper-trading agent built for the Alpaca AI Trading Agents Hackathon. Instead of trusting a single AI-generated trade, the system uses a second model to challenge the proposal before deterministic code decides what is actually allowed to execute.

> **Models make judgments. Code controls authority.**

## How it works
1. Market / portfolio state is collected from Alpaca.
2. A proposer model selects an options trade and explains its reasoning.
3. A second model acts as the Devil's Advocate, returning a failure mode and severity score.
4. The proposer gets at most one revision.
5. A deterministic risk gate enforces hard limits and may generate a safer code-based alternative.
6. A fresh execution check re-reads broker state immediately before submission.
7. The result is either a paper order or NO TRADE.

## Calibration
Severity thresholds were calibrated with a 30-case benchmark:
- Proceed: severity < 0.60
- Revise: 0.60–0.70
- Reject: > 0.70

Results:
- 24 deliberately flawed proposals + 6 clean controls
- 95.8% of flawed cases triggered intervention
- 83.3% of clean controls passed immediately
- Severity AUC: 0.951
- Exact failure-mode match: 12/24 (50%)

## Deterministic risk gates
Code enforces approved underlyings, coverage/collateral, max contracts per trade, max-loss constraints, options exposure limits, daily trade cap, daily drawdown halt, kill switch, and fail-closed state handling.

The gate can also create a **code-generated safer alternative**, such as reducing contract size. Repeated disagreement never forces a trade.

## Alpaca infrastructure
### MCP Server — model-facing context
Structured access to account state, market data, options chains, and research context. The MCP layer is read-oriented and is not the autonomous execution path.

### Trading API — bounded execution
Used for broker state and paper-order submission. Orders only reach this layer after adversarial review, deterministic validation, and a fresh execution-state check.

### CLI — independent audit
Captures account, position, order, and market-clock snapshots before and after automated runs.

## Automation
Daily paper-live runs are automated with GitHub Actions:

```text
gate tests
↓
pre-run Alpaca CLI audit
↓
Devil's Advocate decision
↓
paper order OR no trade
↓
post-run CLI audit
↓
judge report rebuild
↓
artifact preservation
```

For live demos, the same pipeline can be manually triggered with:

```bash
python3 -m agent.run --live
```

Normal runs are automated; the manual command is only for demonstration.

## Evidence
- 28/28 deterministic gate tests passing
- 30-case calibration benchmark
- live and live-dry decision logs
- judge-facing HTML report
- GitHub Actions automation
- Alpaca MCP + CLI + Trading API integration

## Built with
Alpaca Trading API · Alpaca MCP Server · Alpaca CLI · Python · LLM agents · GitHub Actions

## Scope
This project uses an Alpaca paper-trading account for hackathon evaluation. It is not financial advice or a production trading system.
