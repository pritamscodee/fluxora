"""LLM provider wiring and LangSmith tracing.

The model is resolved through one factory so provider choice is configuration, and so
tests can inject a deterministic stub. Nothing else in the agent package imports a
provider SDK directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.core.config import settings

log = logging.getLogger("fluxora.agents.llm")

_TRACING_READY = False


def configure_tracing() -> bool:
    """Enable LangSmith by exporting the variables its tracer reads.

    LangSmith is configured through the environment rather than a client object because
    `langchain-core` picks up tracing globally; setting it here keeps that coupling in one
    place. Returns whether tracing is actually on.
    """
    global _TRACING_READY
    if _TRACING_READY:
        return os.environ.get("LANGSMITH_TRACING") == "true"

    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
        # LangChain still reads the older names in places.
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        log.info("LangSmith tracing enabled, project=%s", settings.langsmith_project)
    else:
        # Explicitly off: a stale LANGSMITH_TRACING in the environment would otherwise
        # make every run try to reach the API and stall on network timeouts.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        if settings.langsmith_tracing:
            log.warning("LANGSMITH_TRACING is on but no API key is set; tracing disabled")

    _TRACING_READY = True
    return os.environ.get("LANGSMITH_TRACING") == "true"


def trace_url(run_id: str) -> str | None:
    if not (settings.langsmith_tracing and settings.langsmith_api_key):
        return None
    return f"https://smith.langchain.com/o/-/projects/p/{settings.langsmith_project}?searchModel=%7B%22filter%22%3A%22{run_id}%22%7D"


class LLMUnavailable(RuntimeError):
    """No usable model is configured. Raised rather than silently degrading, so a missing
    key surfaces as a clear 503 instead of an agent that mysteriously does nothing."""


def build_llm(**overrides: Any):
    """Return a LangChain chat model for the configured provider."""
    configure_tracing()
    provider = overrides.pop("provider", settings.llm_provider).lower()

    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMUnavailable(
                "OPENAI_API_KEY is not set; the agent cannot plan without a model"
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=overrides.pop("model", settings.openai_model),
            api_key=settings.openai_api_key,
            temperature=overrides.pop("temperature", settings.llm_temperature),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            **overrides,
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=overrides.pop("model", settings.anthropic_model),
            api_key=settings.anthropic_api_key,
            temperature=overrides.pop("temperature", settings.llm_temperature),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            **overrides,
        )

    raise LLMUnavailable(f"unknown LLM provider {provider!r}")
