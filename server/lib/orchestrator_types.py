"""Moved out of server/lib/orchestrator.py by tools/refactor/split_apply.py (split 'orchestrator', recipe tools/refactor/splits/s9-orchestrator.toml).
orchestrator.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from . import backends


DEFAULT_PROVIDER = "openai"
JANITOR_ROLE = "message-delegator"
POLICY_OPTION_KEYS = ("fallback_only", "hands_free_only", "confidence_threshold", "timeout_ms", "voice_id")
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_EFFORT = "low"
OPENAI_PROVIDER = "openai"
TYPESAFE_PROVIDER = "typesafe"


# Codex is the catalogue that lists GPT models; the OpenAI API path reuses it.
OPENAI_CATALOG_BACKEND = backends.CODEX
OPENAI_EFFORTS = ("minimal", "low", "medium", "high")
DEFAULT_TIMEOUT_MS = 30000
ORCHESTRATOR_VOICE_ID = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"

KEY_ENABLED = "orchestrator.enabled"
KEY_HANDS_FREE_ONLY = "orchestrator.hands_free_only"
KEY_FALLBACK_ONLY = "orchestrator.fallback_only"
KEY_CONFIDENCE_THRESHOLD = "orchestrator.confidence_threshold"
KEY_PROVIDER = "orchestrator.provider"
KEY_MODEL = "orchestrator.model"
KEY_EFFORT = "orchestrator.effort"
KEY_TIMEOUT_MS = "orchestrator.timeout_ms"
KEY_VOICE_ID = "orchestrator.voice_id"

HIGH_CONFIDENCE = 0.78
STICKY_CONFIDENCE = 0.58
CLARIFY_CONFIDENCE = 0.45
PENDING_TTL_MS = 2 * 60 * 1000
RECENT_MESSAGES_PER_ROLE = 5
MESSAGE_CONTEXT_CHARS = 240
STATE_DETAIL_CONTEXT_CHARS = 240
PENDING_CONTEXT_CHARS = 240
RECENT_AGENT_WINDOW_MS = 30 * 60 * 1000

DECISION_AGENT_MESSAGE = "agent_message"
DECISION_CONTROL = "control"
DECISION_STATUS = "status_query"
DECISION_AGENT_CONTROL = "agent_control"
DECISION_CORRECTION = "recipient_correction"
DECISION_CLARIFY = "clarify"
DECISION_AMBIGUOUS = "ambiguous"
DECISION_IGNORED = "ignored"
DECISION_ERROR = "error"

FINAL_ROUTE = "route"
FINAL_CLARIFY = "clarify"
FINAL_STATUS = "status"
FINAL_CONTROL = "control"
FINAL_IGNORED = "ignored"
FINAL_FALLBACK = "fallback"
FINAL_ERROR = "error"


@dataclass(frozen=True)
class OrchestratorSettings:
    # Off by default — the simple path is just talking to the open agent, with
    # spoken-name fuzzy/regex matching ("hey Mike") for hands-free dictation.
    # The OpenAI router (gpt-5.4-mini) is opt-in via the client toggle.
    enabled: bool = False
    hands_free_only: bool = True
    fallback_only: bool = True
    confidence_threshold: float = HIGH_CONFIDENCE
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    voice_id: str = ORCHESTRATOR_VOICE_ID


@dataclass(frozen=True)
class OrchestratorDecision:
    kind: str
    target_session: str = ""
    confidence: float = 0.0
    addressing: bool = False
    mentioned_sessions: tuple[str, ...] = ()
    name_corrections: tuple[dict[str, Any], ...] = ()
    candidate_scores: tuple[dict[str, Any], ...] = ()
    text_to_send: str = ""
    reason: str = ""
    phrase_key: str = ""
    spoken_text: str = ""
    pending_id: str = ""
    control_action: str = ""
    control_payload: dict[str, Any] | None = None
    status_text: str = ""
    raw: dict[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class OrchestratorOutcome:
    handled: bool
    ok: bool
    session: str
    dispatch: str = ""
    trace_id: str = ""
    action: str = ""
    decision_id: int | None = None
    decision: dict[str, Any] | None = None
    error: str = ""
    status: int = 200
    # Internal, never in a payload: the routed dispatch failed in a way the
    # client's same-id retry may resend (RTO-07).
    retry_dispatch: bool = False


# A routing receipt whose dispatch a same-id retry sends again (RTO-07).
DISPATCH_RETRYABLE = "dispatch_retryable"


# Routing thresholds for the typed provider. They are separate from
# settings.confidence_threshold, which the caller applies to the decision as a
# whole; these decide whether Jev produced a usable decision at all.
TYPESAFE_TARGET_MIN = 0.60     # below this no agent is clearly the right one
TYPESAFE_DECISION_MIN = 0.45   # below this the action itself is unclear
