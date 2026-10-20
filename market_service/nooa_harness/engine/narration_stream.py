"""SSE streaming for narration supervision (Signal B transport).

A thin wrapper over the NOOA unified client: reuses the client's model,
auth, endpoint, and HTTP pool, but consumes the response as a server-sent
event stream instead of a single buffered body. Each yielded delta carries
visible text (the JSON shape forming) plus thinking text (reasoning tokens
— proof the model is working, never part of the parsed shape).

The shim is deliberately dumb: it yields bytes and surfaces transport
errors. All judgment (byte-gap bound, progressive parse, stall verdicts)
lives in the supervision loop (``engine.llm``) and all policy in the
failure FSM. Heartbeat emission is the caller's affair — the shim reports,
it never publishes.

``litellm`` is imported lazily so module import stays free of the
transport cost (same doctrine as ``backends.build_llm``); tests patch
``narration_stream.litellm`` with a scripted async-iterable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

try:  # pragma: no cover — exercised via the patched fake in tests
    import litellm
except ImportError:  # pragma: no cover — narration falls back to buffered acall
    litellm = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamDelta:
    """One streamed step: visible text + thinking text (either may be empty)."""

    text: str = ""
    thinking: str = ""


def _delta_texts(chunk: Any) -> StreamDelta:
    """Extract (text, thinking) from one litellm stream chunk (defensive).

    Standard path is ``choices[0].delta.content``; reasoning providers park
    chain-of-thought on ``delta.reasoning_content`` (attribute name varies —
    read defensively, never raise on shape drift).
    """
    try:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return StreamDelta()
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            # Some providers yield raw dicts.
            if isinstance(choices[0], dict):
                delta = (choices[0].get("delta") or {})
            else:
                return StreamDelta()
        if isinstance(delta, dict):
            text = delta.get("content") or ""
            thinking = delta.get("reasoning_content") or ""
        else:
            text = getattr(delta, "content", None) or ""
            thinking = getattr(delta, "reasoning_content", None) or ""
        return StreamDelta(text=str(text), thinking=str(thinking))
    except Exception:
        log.debug("unparseable stream chunk; counting zero bytes", exc_info=True)
        return StreamDelta()


async def stream_deltas(
    llm: Any,
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float | None = None,
) -> AsyncIterator[StreamDelta]:
    """Yield text deltas from a streaming completion (single attempt).

    Reuses the NOOA client's routing (``llm.model``), credentials/endpoint
    (``llm.config``), and HTTP pool (its async client when present). No
    client-level retry here by doctrine: one attempt, errors surface, the
    substrate owns retries. Raises ``RuntimeError`` when litellm is absent
    (caller falls back to buffered ``acall``) and propagates transport
    errors to the supervision loop.
    """
    if litellm is None:
        raise RuntimeError("litellm is not installed; cannot stream narration")
    config = dict(getattr(llm, "config", None) or {})
    config.pop("timeout", None)
    params: dict[str, Any] = {
        "model": getattr(llm, "model"),
        "messages": list(messages),
        "max_tokens": int(max_tokens),
        "stream": True,
        **config,
    }
    if timeout is not None:
        params["timeout"] = float(timeout)
    http_client = getattr(llm, "_http", None)
    async_client = getattr(http_client, "async_client", None)
    if async_client is not None:
        params.setdefault("client", async_client)
    wrapper = await litellm.acompletion(**params)
    # litellm returns an async-iterable chunk wrapper for stream=True; a
    # non-streaming ModelResponse here means the provider ignored the flag —
    # surface its content once and finish rather than hanging the iterator.
    if not hasattr(wrapper, "__aiter__"):
        yield _delta_texts(wrapper)
        return
    async for chunk in wrapper:
        yield _delta_texts(chunk)


__all__ = ["StreamDelta", "stream_deltas"]
