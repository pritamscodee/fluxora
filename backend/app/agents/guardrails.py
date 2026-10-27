"""Agent guardrails, built on Guardrails AI.

Two layers, deliberately separated:

**Guardrails AI guards** screen natural language — the user's prompt on the way in, the
agent's prose on the way out. Validators are registered locally so they work with no
network access; Hub validators (`hub://guardrails/detect_pii` and friends) are loaded by
name from `GUARDRAILS_INPUT_VALIDATOR_NAMES` / `GUARDRAILS_OUTPUT_VALIDATOR_NAMES` once
installed, so a richer set is opt-in rather than a hard dependency.

**Deterministic policy checks** screen structure — tool allowlist, risk gating, plan and
call ceilings. These are plain Python, never model-mediated and never dependent on an
optional package, because they are the controls that actually stop a runaway or malicious
plan. A guardrail you can uninstall is not a guardrail.

Guardrails AI phones home by default (`enable_metrics` defaults to true with no rc file),
and its telemetry singleton is built at import time — so the opt-out is written before
the library is imported, below.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings

log = logging.getLogger("fluxora.agents.guardrails")


def _opt_out_of_vendor_telemetry() -> None:
    """Write the documented rc opt-out before `guardrails` is imported.

    `HubTelemetry` is constructed during module import and reads `enable_metrics` from
    `~/.guardrailsrc`, which defaults to true when the file is absent. Setting the flag
    afterwards is too late — the exporter already exists and starts posting prompt
    metadata to a third-party endpoint.
    """
    rc = Path(os.path.expanduser("~")) / ".guardrailsrc"
    try:
        if rc.exists() and "enable_metrics" in rc.read_text(encoding="utf-8"):
            return
        existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
        rc.write_text(
            existing.rstrip("\n") + "\nenable_metrics=false\nuse_remote_inferencing=false\n"
            if existing
            else "enable_metrics=false\nuse_remote_inferencing=false\n",
            encoding="utf-8",
        )
        log.info("wrote guardrails telemetry opt-out to %s", rc)
    except OSError as exc:
        log.warning("could not write %s (%s); vendor telemetry may stay enabled", rc, exc)


_opt_out_of_vendor_telemetry()

from guardrails import Guard  # noqa: E402
from guardrails.errors import ValidationError as GuardrailsValidationError  # noqa: E402
from guardrails.settings import settings as gr_settings  # noqa: E402
from guardrails.validators import (  # noqa: E402
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)

gr_settings.disable_tracing = True


class GuardrailViolation(Exception):
    """A guardrail refused to let something proceed."""

    def __init__(self, stage: str, reason: str):
        self.stage = stage
        self.reason = reason
        super().__init__(f"{stage} guardrail: {reason}")


# ---------------------------------------------------------------- validators

# Instruction-override patterns. A heuristic, not a classifier: it catches the blunt
# attempts and is cheap and offline. `hub://guardrails/detect_jailbreak` is the upgrade
# path for anything subtler.
_INJECTION_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions?|prompts?|rules?)",
    r"disregard (?:all |any |the )?(?:previous|prior|above) (?:instructions?|rules?)",
    r"you are (?:now |actually )?(?:a |an )?(?:different|new|unrestricted|dan\b)",
    r"(?:system|developer) prompt",
    r"reveal (?:your |the )?(?:system |initial )?(?:prompt|instructions)",
    r"pretend (?:that )?you (?:are|have)",
    r"without (?:asking|requiring) (?:for )?(?:approval|permission|confirmation)",
    r"bypass (?:the )?(?:approval|guardrail|safety|permission)",
    r"\bsudo\b|\brm -rf\b|\bdrop table\b|;\s*--",
]

# Credentials and connection strings. Checked on the way OUT: a tool result or a traceback
# can carry a secret into prose that is then shown to a user or stored in a trace.
_SECRET_PATTERNS = [
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "OpenAI-style API key"),
    (r"\bsk-ant-[A-Za-z0-9_\-]{16,}", "Anthropic-style API key"),
    (r"\bfx_[A-Za-z0-9]{16,}\b", "Fluxora API key id"),
    (r"\bwhsec_[A-Za-z0-9_\-]{16,}", "webhook signing secret"),
    (r"postgres(?:ql)?://[^\s]*:[^\s]*@", "database connection string"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.", "JWT"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
]

# The agent operates a video platform. Anything with no plausible connection to that is
# out of scope, which blocks the "use the agent as a general chatbot" failure mode.
_IN_SCOPE_TERMS = {
    "video", "asset", "assets", "clip", "clips", "upload", "uploads", "thumbnail",
    "thumbnails", "caption", "captions", "subtitle", "subtitles", "transcode",
    "transcoding", "encode", "encoding", "playback", "stream", "streaming", "hls",
    "mp4", "rendition", "renditions", "download", "downloads", "summary", "summarise",
    "summarize", "chapter", "chapters", "duration", "resolution", "project", "usage",
    "webhook", "webhooks", "delete", "remove", "list", "show", "find", "status",
    "ready", "errored", "audio", "track", "tracks", "media", "player", "gif",
    "storyboard", "transcript", "transcribe", "signed", "public", "library",
}


@register_validator(name="fluxora/no_prompt_injection", data_type="string")
class NoPromptInjection(Validator):
    """Reject instruction-override attempts in free text."""

    def _validate(self, value: Any, metadata: dict | None = None) -> ValidationResult:
        text = str(value or "").lower()
        for pattern in _INJECTION_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return FailResult(
                    error_message=(
                        "the request appears to try to override the agent's instructions "
                        f"({match.group(0)!r})"
                    )
                )
        return PassResult()


@register_validator(name="fluxora/in_scope", data_type="string")
class InScope(Validator):
    """Require the request to plausibly concern video operations."""

    def _validate(self, value: Any, metadata: dict | None = None) -> ValidationResult:
        words = set(re.findall(r"[a-z]+", str(value or "").lower()))
        if words & _IN_SCOPE_TERMS:
            return PassResult()
        return FailResult(
            error_message=(
                "the request does not appear to be about video assets, uploads, "
                "playback, captions or usage; this agent only operates the video platform"
            )
        )


@register_validator(name="fluxora/no_secret_leakage", data_type="string")
class NoSecretLeakage(Validator):
    """Block credentials from appearing in agent output."""

    def _validate(self, value: Any, metadata: dict | None = None) -> ValidationResult:
        text = str(value or "")
        for pattern, label in _SECRET_PATTERNS:
            if re.search(pattern, text):
                return FailResult(error_message=f"output contains what looks like a {label}")
        return PassResult()


@register_validator(name="fluxora/bounded_length", data_type="string")
class BoundedLength(Validator):
    def __init__(self, max_chars: int = 8000, **kwargs):
        super().__init__(**kwargs)
        self.max_chars = max_chars

    def _validate(self, value: Any, metadata: dict | None = None) -> ValidationResult:
        length = len(str(value or ""))
        if length > self.max_chars:
            return FailResult(
                error_message=f"text is {length} characters, over the {self.max_chars} limit"
            )
        return PassResult()


# ---------------------------------------------------------------- hub validators

def _hub_validators(names: list[str]) -> list[Validator]:
    """Instantiate Hub validators that are actually installed.

    Hub validators need `guardrails hub install hub://<name>` and, for some, a model
    download. A missing one is logged and skipped rather than failing startup, so a
    deployment without Hub access still runs with the local validators.
    """
    loaded: list[Validator] = []
    for raw in names:
        name = raw.strip().removeprefix("hub://")
        if not name:
            continue
        attr = name.split("/")[-1]
        class_name = "".join(part.title() for part in attr.split("_"))
        try:
            module = __import__("guardrails.hub", fromlist=[class_name])
            validator_cls = getattr(module, class_name)
            loaded.append(validator_cls(on_fail="exception"))
            log.info("loaded guardrails hub validator %s", name)
        except (ImportError, AttributeError) as exc:
            log.warning(
                "guardrails hub validator %r is not installed (%s); "
                "run `guardrails hub install hub://%s` to enable it",
                name, exc.__class__.__name__, name,
            )
    return loaded


_input_guard: Guard | None = None
_output_guard: Guard | None = None


def input_guard() -> Guard:
    global _input_guard
    if _input_guard is None:
        guard = Guard(name="fluxora-agent-input").use(
            BoundedLength(max_chars=settings.agent_max_prompt_chars, on_fail="exception"),
            NoPromptInjection(on_fail="exception"),
            InScope(on_fail="exception"),
            *_hub_validators(settings.guardrails_input_validators),
        )
        _input_guard = guard
    return _input_guard


def output_guard() -> Guard:
    global _output_guard
    if _output_guard is None:
        _output_guard = Guard(name="fluxora-agent-output").use(
            NoSecretLeakage(on_fail="exception"),
            BoundedLength(max_chars=20000, on_fail="exception"),
            *_hub_validators(settings.guardrails_output_validators),
        )
    return _output_guard


def check_input(prompt: str) -> str:
    """Screen a user prompt. Raises GuardrailViolation if it must not reach the model."""
    try:
        input_guard().validate(prompt)
    except GuardrailsValidationError as exc:
        log.warning("input guardrail blocked a prompt: %s", exc)
        raise GuardrailViolation("input", str(exc)) from exc
    return prompt


def check_output(text: str) -> str:
    """Screen agent prose before it is returned or persisted."""
    try:
        output_guard().validate(text)
    except GuardrailsValidationError as exc:
        log.error("output guardrail blocked a response: %s", exc)
        raise GuardrailViolation("output", str(exc)) from exc
    return text


def redact(text: str) -> str:
    """Best-effort scrub, for log and trace paths that must not raise."""
    scrubbed = text
    for pattern, label in _SECRET_PATTERNS:
        scrubbed = re.sub(pattern, f"[redacted {label}]", scrubbed)
    return scrubbed


# ---------------------------------------------------------------- policy checks

@dataclass
class PolicyDecision:
    """Outcome of the structural checks on a proposed plan."""

    allowed: bool
    requires_approval: bool = False
    reasons: list[str] = field(default_factory=list)
    high_risk_steps: list[str] = field(default_factory=list)


def check_plan(plan: list[dict]) -> PolicyDecision:
    """Validate a proposed plan's structure, tools, and arguments before anything runs.

    Non-negotiable and model-independent. Every step must name a registered tool and
    carry arguments that satisfy that tool's Pydantic schema; any step whose tool is
    expensive or destructive forces the whole run through human approval.
    """
    from app.agents.tools import REGISTRY, ToolError, validate_args

    decision = PolicyDecision(allowed=True)

    if not plan:
        return PolicyDecision(allowed=False, reasons=["the plan is empty"])
    if len(plan) > settings.agent_max_plan_steps:
        return PolicyDecision(
            allowed=False,
            reasons=[
                f"the plan has {len(plan)} steps, over the limit of "
                f"{settings.agent_max_plan_steps}"
            ],
        )

    for index, step in enumerate(plan, start=1):
        name = (step or {}).get("tool")
        if name not in REGISTRY:
            decision.allowed = False
            decision.reasons.append(f"step {index} names an unknown tool {name!r}")
            continue
        try:
            validate_args(name, (step or {}).get("args") or {})
        except ToolError as exc:
            decision.allowed = False
            decision.reasons.append(f"step {index}: {exc}")
            continue
        if REGISTRY[name].risk.needs_approval:
            decision.requires_approval = True
            decision.high_risk_steps.append(f"step {index}: {name}")

    if decision.requires_approval and not settings.agent_require_approval:
        # Only an explicit operator decision can turn this off, and it is loud.
        log.warning(
            "AGENT_REQUIRE_APPROVAL is disabled; running high-risk steps unattended: %s",
            decision.high_risk_steps,
        )
        decision.requires_approval = False

    return decision


def check_tool_call(name: str, calls_so_far: int) -> None:
    """Final gate immediately before a tool executes."""
    from app.agents.tools import REGISTRY

    if name not in REGISTRY:
        raise GuardrailViolation("tool", f"{name!r} is not a registered tool")
    if calls_so_far >= settings.agent_max_tool_calls:
        raise GuardrailViolation(
            "tool",
            f"this run has already made {calls_so_far} tool calls, the maximum is "
            f"{settings.agent_max_tool_calls}",
        )
