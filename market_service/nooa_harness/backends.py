"""Model backend configuration for the optional NOOA environment.

Canonical env-var scheme is ``NOOA_MODEL_*`` / ``NOOA_API_KEY``. The legacy
``PROVIDER`` / ``PROVIDER_API_KEY`` / ``PROVIDER_BASE_URL`` aliases are still
read as fallbacks during the transition but are deprecated.

Only ``build_llm()`` imports ``nooa`` / litellm, so importing this module
(and running the contract/runner/persistence tests) never pays litellm's
import cost.

Transport: ``build_llm()`` returns a ``nooa.unifiedllm`` client
(``CompletionClient`` / ``ResponsesClient``) built on the LITELLM framework.
This is the upstream NOOA-native transport. The model id in ``routed_model()``
carries its own litellm provider prefix where needed (e.g.
``openrouter/deepseek/deepseek-chat``), and ``NOOA_MODEL_BASE_URL`` /
``NOOA_API_KEY`` are passed to litellm as ``api_base`` / ``api_key``
overrides. To change the model an operator edits only the env layer:
``NOOA_MODEL_NAME`` (the model id / routing string),
``NOOA_MODEL_BASE_URL`` (the endpoint surface), and ``NOOA_API_KEY`` (the
credential for that surface). ``provider`` is informational and carried on
the config for observability; litellm + unifiedllm handle provider routing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelBackendConfig:
    """Validated model-endpoint config for the analyst suite."""

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.2
    # Name of the env var the api_key was sourced from (for observability /
    # test assertions). Never contains the secret value.
    api_key_env: str | None = None

    @classmethod
    def from_env(cls) -> "ModelBackendConfig":
        # Canonical: NOOA_MODEL_PROVIDER. Fallback: PROVIDER (deprecated).
        provider = (
            os.getenv("NOOA_MODEL_PROVIDER")
            or os.getenv("PROVIDER")
            or "openai"
        ).strip()

        model = os.getenv("NOOA_MODEL_NAME")
        if not model:
            raise ValueError(
                "NOOA_MODEL_NAME is required (the model id for the configured endpoint)"
            )

        base_url = (
            os.getenv("NOOA_MODEL_BASE_URL")
            or os.getenv("PROVIDER_BASE_URL")
            or None
        )

        # Canonical: NOOA_API_KEY. Fallback: PROVIDER_API_KEY (deprecated).
        if os.getenv("NOOA_API_KEY"):
            api_key_source = "NOOA_API_KEY"
        elif os.getenv("PROVIDER_API_KEY"):
            api_key_source = "PROVIDER_API_KEY"
        else:
            api_key_source = None
        api_key = os.getenv(api_key_source) if api_key_source else None

        temperature = float(os.getenv("NOOA_MODEL_TEMPERATURE", "0.2"))
        if not 0 <= temperature <= 2:
            raise ValueError("NOOA_MODEL_TEMPERATURE must be between 0 and 2")

        return cls(
            provider=provider,
            model=model.strip(),
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            api_key_env=api_key_source,
        )

    def routed_model(self) -> str:
        """Return the model id / litellm routing string for the endpoint.

        If ``NOOA_MODEL_NAME`` already carries a provider prefix (contains
        ``/``) it is passed through verbatim — e.g.
        ``openrouter/deepseek/deepseek-chat``. Otherwise the configured
        ``NOOA_MODEL_PROVIDER`` is used to prefix the model so litellm can
        route it. This is what fixes "native" / local models:

        * ``openai`` / ``vllm``  -> ``openai/<model>`` (OpenAI-compatible)
        * ``ollama``             -> ``ollama/<model>``
        * otherwise              -> ``<provider>/<model>``
        """
        raw = self.model.strip()
        if "/" in raw:
            return raw
        provider = (self.provider or "openai").strip().lower()
        if provider in ("openai", "vllm"):
            return f"openai/{raw}"
        if provider == "ollama":
            return f"ollama/{raw}"
        if provider in ("ollama_chat", "ollama-chat"):
            return f"ollama_chat/{raw}"
        return f"{provider}/{raw}" if provider else raw

    def build_llm(self):
        """Build NOOA's unified LLM client only when a run actually starts.

        This is the ONLY call site that imports ``nooa`` / litellm, keeping
        module import, contract tests, and the runner's read path free of the
        litellm import cost.
        """
        from nooa.unifiedllm.registry import get_llm_client

        options: dict[str, Any] = {"temperature": self.temperature}
        if self.api_key:
            options["api_key"] = self.api_key
        if self.base_url:
            options["api_base"] = self.base_url
        return get_llm_client(self.routed_model(), **options)


__all__ = ["ModelBackendConfig"]