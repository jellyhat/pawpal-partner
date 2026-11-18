"""Thin wrapper around the Anthropic SDK.

Claude is optional: if no credentials are configured (or
``PAWPAL_DISABLE_LLM=1``), ``get_client()`` returns ``None`` and every AI
feature falls back to a deterministic, offline path. Tests inject a fake
client with the same ``beta.messages.create`` shape.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("pawpal.llm")

MODEL = os.environ.get("PAWPAL_MODEL", "claude-opus-5-5")
# Server-side refusal fallback: if the main model declines, the API re-runs
# the request on a fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def get_client() -> Optional[Any]:
    """Return an Anthropic client, or None when the app should run offline."""
    if os.environ.get("PAWPAL_DISABLE_LLM") == "1":
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        logger.info("No Anthropic credentials found; AI features run in offline mode.")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; running offline.")
        return None
    return anthropic.Anthropic(max_retries=2, timeout=120.0)


def create_message(client: Any, **kwargs: Any) -> Any:
    """Call the Messages API with the project's defaults and log usage."""
    params = {
        "model": MODEL,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }
    params.update(kwargs)
    response = client.beta.messages.create(**params)
    usage = getattr(response, "usage", None)
    logger.info(
        "Claude call: model=%s stop=%s in=%s out=%s",
        params["model"],
        getattr(response, "stop_reason", "?"),
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )
    return response


def response_text(response: Any) -> str:
    """Join the text blocks of a response."""
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
