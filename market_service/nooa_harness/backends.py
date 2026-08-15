"""Model backend configuration for the optional NOOA environment.

Canonical env-var scheme is ``NOOA_MODEL_*`` / ``NOOA_API_KEY``. The legacy
``PROVIDER`` / ``PROVIDER_API_KEY`` / ``PROVIDER_BASE_URL`` aliases are still
read as fallbacks during the transition but are deprecated.

Only ``build_llm()`` imports ``nooa`` / litellm, so importing this module
(and running the contract/runner/persistence tests) never pays litellm's
import cost.

Provider routing — the ``routed_model()`` method maps a canonical provider
token to the litellm model prefix that selects the right thin provider SDK:

    openai    -> openai/<model>     (OpenAI-compatible gateways, vLLM)
    ollama    -> ollama/<model>     (local Apple Silicon)
    anthropic -> anthropic/<model>  (Anthropic API or compatible gateway)
    minimax   -> minimax/<model>    (litellm native minimax provider, /v1)
    <other>   -> passthrough         (model name must carry its own prefix)

As a convenience, a base URL ending in ``/anthropic`` routes an unprefixed
model to ``anthropic/`` — this covers Anthropic-compatible gateways whose
vendor name is not litellm-native (e.g. a minimax anthropic endpoint).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Canonical provider tokens -> litellm model prefix.
_PROVIDER_PREFIX: dict[str, str] = {
    "openai": "openai/",
    "vllm": "openai/",
    "ollama": "ollama/",
    "anthropic": "anthropic/",
    "minimax": "minimax/",
}

_KNOWN_PREFIXES = tuple(sorted(_PROVIDER_PREFIX.values()))


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
        """Return the litellm model string for the configured endpoint."""
        model = self.model.strip()
        # If the model name already carries a litellm prefix, respect it.
        if any(model.startswith(prefix) for prefix in _KNOWN_PREFIXES):
            return model
        provider = self.provider.strip().lower()
        if provider in _PROVIDER_PREFIX:
            return f"{_PROVIDER_PREFIX[provider]}{model}"
        # Convenience heuristic for Anthropic-compatible gateways whose base
        # URL carries /anthropic (e.g. an internal minimax/vertex path).
        if self.base_url and self.base_url.rstrip("/").endswith("/anthropic"):
            return f"anthropic/{model}"
        # Unknown vendor -> litellm passthrough; routing is decided by the
        # model prefix the operator supplied.
        return model

    def build_llm(self):
        """Build NOOA's unified model client only when a run actually starts.

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