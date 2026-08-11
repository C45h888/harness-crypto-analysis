# Crypto AI Analytics

Live market data pipeline combining **Binance Spot** public APIs (order flow) with **CryptoQuant** on-chain analytics.

## Stack

- **Binance Spot** — orderbook, trades, ticker, klines (no API key needed for public market data)
- **CryptoQuant MCP** — on-chain metrics (MVRV, SOPR, exchange flows, etc.) via the `cryptoquant-mcp` npm server
- **Python 3.12** with `requests` + `websockets` (already installed in `.venv`)
- **Hermes Agent** — wraps the CryptoQuant server as MCP tools and runs this code through `execute_code`

## Files

| File | Purpose |
|---|---|
| `binance_client.py` | REST client for Binance Spot public endpoints |
| `binance_ws.py` | WebSocket streams (trades, kline, bookTicker) + `BookSnapshot` L2 book |
| `order_flow.py` | CVD, OBI, VWAP, bucketed CVD, whale-flagging, flow_summary |
| `cryptoquant_client.py` | Direct subprocess bridge to `cryptoquant-mcp` (parity with the MCP gateway path) |
| `market_analysis.py` | Orchestrator: combined Binance + CryptoQuant snapshot, CLI entry |
| `.env.example` | API key template |

## Quickstart

```bash
cd /Users/kamii/Documents/crypto-ai-anal
source .venv/bin/activate
cp .env.example .env
# edit .env to add CRYPTOQUANT_API_KEY (and Binance keys only for private endpoints)

# one-shot snapshot (BTC orderflow + CryptoQuant metric descriptions)
python market_analysis.py BTCUSDT

# JSON output
python market_analysis.py BTCUSDT --json

# live websocket mode (prints every 60s)
python market_analysis.py BTCUSDT --continuous --window 60
```

## Smoke tests

```bash
python binance_client.py          # REST ping
python binance_ws.py              # print 5 raw trades + 5-trade CVD
python cryptoquant_client.py describe mvrv
python cryptoquant_client.py list
```

## What the pipeline measures

| Layer | Source | Metric |
|---|---|---|
| Microstructure | Binance | CVD (Cumulative Volume Delta) |
| Microstructure | Binance | OBI (Order Book Imbalance, top-20 levels) |
| Microstructure | Binance | VWAP + stddev |
| Microstructure | Binance | Trade distribution (buy/sell ratio) |
| On-chain | CryptoQuant | MVRV, SOPR, exchange netflow, funding, whale ratio |

**Basic-plan note:** CryptoQuant free/basic currently locks all numeric data endpoints. `describe_metric` still returns thresholds + interpretations which feed into the structured summary. Upgrade to professional to unlock numerics.

## Hermes integration

When run inside Hermes Agent:
- Use `mcp_cryptoquant_*` tools for direct MCP access (no env var needed — key already in `~/.hermes/config.yaml`)
- Use `execute_code` to call `binance_client.py` / `order_flow.py` directly
- Use `delegate_task` to spawn subagents that pull Binance + CQ in parallel
