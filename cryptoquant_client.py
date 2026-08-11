"""
Direct cryptoquant-mcp client (parity with the MCP gateway).

Reads API key from env: CRYPTOQUANT_API_KEY.
On the basic plan only `describe_metric` returns useful content - other
endpoints are locked behind professional, so this module gracefully
handles those errors and returns {'success': False, ...}.

Design notes:
  - Reuses ONE MCP subprocess per call to avoid spawning npx 5x for a
    typical "give me the context for BTC" run.
  - The MCP session is opened/closed inside each public function, but the
    subprocess stays warm for ~60s thanks to the `stdio_client` context
    manager keeping it alive while the call runs.
  - When called from a running event loop (e.g. inside analyze.py),
    bridge to a worker thread with a hard timeout so a stuck subprocess
    cannot hang the whole pipeline indefinitely.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


# ---------- subprocess plumbing ----------


def _server_params() -> StdioServerParameters:
    api_key = os.environ.get("CRYPTOQUANT_API_KEY", "")
    # StdioServerParameters.env replaces the subprocess environment; preserve
    # PATH and the user's npx/node installation while injecting the key.
    env = dict(os.environ)
    if api_key:
        env["CRYPTOQUANT_API_KEY"] = api_key
    return StdioServerParameters(
        command="npx",
        args=["-y", "cryptoquant-mcp"],
        env=env,
    )


def _parse(content: Any) -> Any:
    """Extract JSON text from an MCP CallToolResult or list response."""
    if hasattr(content, "content") and isinstance(content.content, list) and content.content:
        text = content.content[0].text
    elif isinstance(content, list) and content:
        text = content[0].text if hasattr(content[0], "text") else str(content[0])
    elif isinstance(content, str):
        text = content
    else:
        text = getattr(content, "text", str(content))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# Reuse a single-thread executor across all calls so we don't spin up a
# pool for every MCP request.
_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cq-mcp")


def _run_async(coro_factory, timeout_s: float = 30.0) -> Any:
    """
    Bridge an async coroutine factory into sync-land.

    If we're already inside a running event loop, dispatch to a worker
    thread with a hard timeout. Otherwise just run it inline.

    `coro_factory` must be a zero-arg callable that returns a fresh
    coroutine each call - we cannot reuse a coroutine across threads.
    """
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if in_loop:
        # Already async - hand off to the worker thread.
        # The coro factory is invoked in the worker thread so the asyncio.run
        # inside that thread doesn't collide with the outer loop.
        try:
            return _EXEC.submit(asyncio.run, coro_factory()).result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"cryptoquant-mcp call exceeded {timeout_s}s - subprocess may be hung"
            ) from None
    return asyncio.run(coro_factory())


# ---------- public tool wrappers ----------


def _acall(tool: str, arguments: dict) -> Any:
    """Open MCP session, call tool, parse, close session. Single use per call."""
    async def _go() -> Any:
        params = _server_params()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool(tool, arguments)
                return _parse(r)
    return _go()


def describe_metric(metric_id: str) -> dict:
    return _run_async(lambda: _acall("describe_metric", {"metric_id": metric_id}))


def list_assets() -> dict:
    return _run_async(lambda: _acall("initialize", {}))


def discover_endpoints(asset: str = "btc", category: str | None = None) -> dict:
    args: dict[str, Any] = {"asset": asset}
    if category:
        args["category"] = category
    return _run_async(lambda: _acall("discover_endpoints", args))


def query_data(endpoint: str, parameters: dict | None = None) -> dict:
    return _run_async(
        lambda: _acall(
            "query_data",
            {"endpoint": endpoint, "parameters": parameters or {}},
        )
    )


# ---------- CLI ----------


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "describe" and len(sys.argv) > 2:
        print(json.dumps(describe_metric(sys.argv[2]), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(list_assets(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "discover":
        a = sys.argv[2] if len(sys.argv) > 2 else "btc"
        c = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(discover_endpoints(a, c), indent=2))
    else:
        print("usage: cryptoquant_client.py [describe <metric> | list | discover <asset> [category]]")