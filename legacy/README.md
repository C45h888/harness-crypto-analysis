# legacy/

Archived, single-purpose market tools that predate the `market_service` package.

**Convention (single runtime state):** the `market_service/` package is the one
authoritative implementation (clients, calculations, analysis, persistent
collector, and the harness snapshot contract). Nothing in `market_service/`
imports from here. These scripts exist for exploratory/live-session use and are
kept in one place so the project root stays clean and coherent.

## How to run

The repo root must be the working directory so `market_service` resolves:

```bash
cd /Users/kamii/Documents/crypto-ai-anal
.venv/bin/python legacy/<script>.py [args]
```

> The handful of older scripts that used to `import binance` / `import flow`
> directly have been rewired to the canonical `market_service.clients.binance`
> and `market_service.calculations.flow` modules. Their import headers insert
> the repo root onto `sys.path` automatically, so no `PYTHONPATH` is needed.

## Classification

| Status | File | Notes / duplicate of |
|---|---|---|
| **Compatibility wrappers** | `binance.py`, `flow.py`, `analyze.py` | Thin re-exports of `market_service.clients.binance`, `market_service.calculations.flow`, and `market_service.analysis.market`. Retired in favour of the package entrypoints. |
| **Kept as exploratory tool** | `auction_dynamics.py` | Rewired to `market_service`; corrupt module docstring fixed. |
| **Kept as exploratory tool** | `demand_diagnostic.py` | Rewired to `market_service`. |
| **Kept as exploratory tool** | `session_regime.py` | Rewired to `market_service`. |
| **Kept as exploratory tool** | `sol_futures.py` | Rewired to `market_service`. |
| **Kept as exploratory tool** | `continue_monitor.py` | Stdlib-only long-running monitor. |
| **Kept as exploratory tool** | `deep_keystone.py` | Stdlib-only keystone depth read. |
| **Kept as exploratory tool** | `flow5m.py` | Stdlib-only 5m flow. |
| **Kept as exploratory tool** | `keystone_scan.py` | Stdlib-only keystone scan. |
| **Kept as exploratory tool** | `long_term_flow.py` | Stdlib-only long-horizon flow. |
| **Kept as exploratory tool** | `oi_analysis_run.py` | Stdlib-only; run wrapper around OI analysis. |
| **Kept as exploratory tool** | `oi_analysis.py` | Stdlib-only; **superseded** by `market_service.analysis.oi`. |
| **Kept as exploratory tool** | `path_absorption.py` | Stdlib-only path/fuel read. |
| **Kept as exploratory tool** | `scan_levels.py` | Stdlib-only level scan. |
| **Kept as exploratory tool** | `seller_wall_check.py` | Stdlib-only wall check. |
| **Kept as exploratory tool** | `spot_fut_assess.py` | Stdlib-only spot-vs-futures assessment. |
| **Kept as exploratory tool** | `summarize_monitor.py` | Stdlib-only monitor summarizer. |
| **Kept as exploratory tool** | `wall_analysis.py` | Stdlib-only wall analysis. |
| **Kept as exploratory tool** | `wall_state_check.py` | Stdlib-only wall state check. |
| **Kept as exploratory tool** | `liquidations.py` | Stdlib-only; **superseded** by `market_service.analysis.liquidations`. |
| **Kept as exploratory tool** | `macro.py` | Stdlib-only; **superseded** by `market_service.analysis.macro`. |
| **Legacy-only (monitors / CQ bridge)** | `sol_deep_monitor.py`, `sol_monitor_alerts.py`, `sol_deep_monitor.sh` | `aiohttp` long-running monitors. |
| **Legacy-only (CQ bridge)** | `cryptoquant_client.py` | Older standalone MCP bridge; package equivalent lives at `market_service.clients.cryptoquant`. |
| **Runtime data** | `data/*.json` | Historical runtime logs (`monitor_log`, `sol_deep_log`, `sol_session_log`, `path_absorption_log`). Not loaded by any code path. |

### Superseded scripts

Three legacy scripts have equivalent formal modules inside `market_service/`:

- `oi_analysis.py`        -> `market_service.analysis.oi`
- `liquidations.py`       -> `market_service.analysis.liquidations`
- `macro.py`              -> `market_service.analysis.macro`

Prefer the package modules through the unified snapshot
(`market_service.commands.snapshot`) for anything new. The standalone scripts are
kept for byte-for-byte historical comparison during migration.