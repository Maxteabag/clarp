"""Hands-free LLM router for deciding where a spoken turn should go.

The name router (lib.routing) answers one narrow question: did the first few
words look like an agent name? The hands-free orchestrator is the safer path
for dictation: it sees every active agent, recent per-agent context, pending
clarifications, and the sticky focus before deciding whether the user is
addressing an agent, mentioning one, asking for status, or issuing a control
command.
"""
from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

from . import agents as agents_db
from . import backends, config, eventlog, settings_store
from .db import conn, now_ms
from .log import log_exception
from .prompt_admissions import PromptAdmission
from . import prompt_admissions
from .protocol import SSEType


DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_EFFORT = "low"
OPENAI_PROVIDER = "openai"
_OPENAI_ALIASES = {"openai", "gpt"}
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

_JSON_RE = re.compile(r"\{.*\}", re.S)


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


def normalize_provider(raw: str | None) -> str:
    """Canonical routing provider id: a registry backend id or "openai".

    Aliases the registry knows (antigravity, claude-code, open-code) and the
    OpenAI spellings collapse; anything else is returned lower-cased so the
    caller can reject it.
    """
    value = (raw or "").strip().lower()
    if value in _OPENAI_ALIASES:
        return OPENAI_PROVIDER
    if value == "claude-code":
        return backends.CLAUDE
    adapter = backends.get(value)
    return adapter.id if adapter else value


def provider_options() -> list[dict[str, Any]]:
    """Routing providers this Host can run: catalogue backends plus OpenAI.

    Served on /orchestrator/settings so the clients' provider dropdown is the
    registry, not a bundled enum. ``installed`` is a cheap PATH check, not the
    full capability probe; ``effort_options`` null means "derive from the
    /agent-model-options row named by catalog_backend".
    """
    rows: list[dict[str, Any]] = []
    for adapter in backends.routing_adapters():
        binary = adapter.required_binary
        if adapter.id == backends.CLAUDE:
            from . import clarp_runner
            binary = clarp_runner.configured_claude_bin()
        rows.append({
            "id": adapter.id,
            "label": adapter.label,
            "detail": f"Runs an isolated {adapter.label} request on this Host.",
            "kind": "backend",
            "catalog_backend": adapter.id,
            "installed": shutil.which(binary) is not None,
            "effort_options": None,
        })
    rows.append({
        "id": OPENAI_PROVIDER,
        "label": "OpenAI API",
        "detail": "Uses the configured OpenAI API key directly.",
        "kind": "api",
        "catalog_backend": OPENAI_CATALOG_BACKEND,
        "installed": bool(config.load().openai_key()),
        "effort_options": list(OPENAI_EFFORTS),
    })
    return rows


def is_routing_provider(raw: str | None) -> bool:
    provider = normalize_provider(raw)
    if provider == OPENAI_PROVIDER:
        return True
    adapter = backends.get(provider)
    return adapter is not None and adapter.supports_routing


def addressing_status() -> dict:
    """Current mode plus the catalogue, so the app can draw the picker."""
    from . import addressing
    return {"mode": addressing.mode(), "modes": list(addressing.MODES)}


def get_settings() -> OrchestratorSettings:
    cfg = config.load()
    provider_default = DEFAULT_PROVIDER
    provider = settings_store.get_text(KEY_PROVIDER, default=provider_default).strip()
    provider = provider or provider_default
    canonical = normalize_provider(provider)
    if canonical == backends.AGY:
        model_default = cfg.agy_model.strip()
    elif canonical == OPENAI_PROVIDER:
        model_default = DEFAULT_MODEL
    else:
        model_default = ""
    effort_default = DEFAULT_EFFORT if canonical == OPENAI_PROVIDER else ""
    timeout_raw = settings_store.get_text(
        KEY_TIMEOUT_MS, default=str(DEFAULT_TIMEOUT_MS)
    ).strip()
    try:
        timeout_ms = max(250, min(60000, int(timeout_raw)))
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    confidence_raw = settings_store.get_text(
        KEY_CONFIDENCE_THRESHOLD, default=str(HIGH_CONFIDENCE)
    ).strip()
    try:
        confidence_threshold = max(0.5, min(0.99, float(confidence_raw)))
    except ValueError:
        confidence_threshold = HIGH_CONFIDENCE
    return OrchestratorSettings(
        enabled=settings_store.get_bool(KEY_ENABLED, default=False),
        hands_free_only=settings_store.get_bool(KEY_HANDS_FREE_ONLY, default=True),
        fallback_only=settings_store.get_bool(KEY_FALLBACK_ONLY, default=True),
        confidence_threshold=confidence_threshold,
        provider=provider,
        model=settings_store.get_text(KEY_MODEL, default=model_default).strip(),
        effort=settings_store.get_text(KEY_EFFORT, default=effort_default).strip(),
        timeout_ms=timeout_ms,
        voice_id=settings_store.get_text(
            KEY_VOICE_ID, default=ORCHESTRATOR_VOICE_ID
        ).strip() or ORCHESTRATOR_VOICE_ID,
    )


def update_settings(data: dict[str, Any]) -> OrchestratorSettings:
    if "enabled" in data:
        settings_store.set_bool(KEY_ENABLED, bool(data.get("enabled")))
    if "hands_free_only" in data:
        settings_store.set_bool(KEY_HANDS_FREE_ONLY, bool(data.get("hands_free_only")))
    if "fallback_only" in data:
        settings_store.set_bool(KEY_FALLBACK_ONLY, bool(data.get("fallback_only")))
    if "confidence_threshold" in data:
        try:
            threshold = max(0.5, min(0.99, float(data.get("confidence_threshold"))))
        except (TypeError, ValueError):
            threshold = HIGH_CONFIDENCE
        settings_store.set_text(KEY_CONFIDENCE_THRESHOLD, str(threshold))
    if "provider" in data:
        provider = str(data.get("provider") or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
        if not is_routing_provider(provider):
            raise ValueError(f"unsupported orchestrator provider: {provider}")
        settings_store.set_text(KEY_PROVIDER, normalize_provider(provider))
    if "model" in data:
        settings_store.set_text(KEY_MODEL, str(data.get("model") or "").strip())
    if "effort" in data:
        settings_store.set_text(KEY_EFFORT, str(data.get("effort") or "").strip())
    if "timeout_ms" in data:
        try:
            timeout_ms = max(250, min(60000, int(data.get("timeout_ms"))))
        except (TypeError, ValueError):
            timeout_ms = DEFAULT_TIMEOUT_MS
        settings_store.set_text(KEY_TIMEOUT_MS, str(timeout_ms))
    if "voice_id" in data:
        settings_store.set_text(KEY_VOICE_ID, str(data.get("voice_id") or "").strip())
    return get_settings()


def should_run(
    settings: OrchestratorSettings,
    *,
    hands_free: bool,
    fallback_request: bool = False,
) -> bool:
    return bool(
        settings.enabled
        and (hands_free or not settings.hands_free_only)
        and (fallback_request or not settings.fallback_only)
    )


def _should_scan_broader(
    decision: OrchestratorDecision,
    packet: dict[str, Any],
    requested_session: str,
) -> bool:
    if packet.get("context_scope") != "focused":
        return False
    if decision.kind == DECISION_ERROR:
        return False
    if decision.kind == DECISION_IGNORED:
        return decision.confidence < 0.85
    target = decision.target_session.strip()
    if target and target != requested_session:
        return True
    if decision.kind in {DECISION_CLARIFY, DECISION_AMBIGUOUS}:
        return True
    if decision.kind == DECISION_AGENT_MESSAGE:
        return decision.confidence < STICKY_CONFIDENCE
    if decision.kind == DECISION_STATUS:
        return not target or target != requested_session
    return False


def record_routing_message(*, session: str, role: str, text: str,
                           trace_id: str = "", source: str = "orchestrator") -> None:
    agent = agents_db.get_by_session(session)
    if not agent or not text.strip():
        return
    conn().execute(
        """INSERT INTO agent_routing_messages
              (agent_id, session, role, text, trace_id, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            agent["agent_id"],
            session,
            role,
            text.strip(),
            trace_id,
            source,
            now_ms(),
        ),
    )


def recent_decisions(limit: int = 30) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT * FROM orchestrator_decisions
            ORDER BY created_at DESC, decision_id DESC
            LIMIT ?""",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [_decision_row_to_dict(r) for r in rows]


def recent_ignored_decisions(limit: int = 30) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT * FROM orchestrator_decisions
            WHERE final_action = ?
            ORDER BY created_at DESC, decision_id DESC
            LIMIT ?""",
        (FINAL_IGNORED, max(1, min(limit, 200))),
    ).fetchall()
    return [_decision_row_to_dict(r) for r in rows]


def _decision_row_to_dict(row) -> dict[str, Any]:
    out = dict(row)
    for key in (
        "mentioned_sessions_json",
        "name_corrections_json",
        "candidate_scores_json",
        "raw_response_json",
    ):
        try:
            out[key[:-5] if key.endswith("_json") else key] = json.loads(row[key] or "{}")
        except json.JSONDecodeError:
            out[key[:-5] if key.endswith("_json") else key] = None
    return out


class OrchestratorService:
    def __init__(
        self,
        ctx,
        *,
        model_call: Callable[[dict[str, Any], OrchestratorSettings], dict[str, Any]]
        | None = None,
        now: Callable[[], int] = now_ms,
    ):
        self.ctx = ctx
        self.model_call = model_call or call_model
        self.now = now

    def handle_send(
        self,
        *,
        text: str,
        requested_session: str,
        trace_id: str,
        prompt_admission: PromptAdmission | None = None,
        hands_free: bool,
        synthesize_audio: bool,
        unheard_audio_sessions: tuple[str, ...] = (),
        dispatch: Callable[..., Any],
        fallback_request: bool = False,
    ) -> OrchestratorOutcome | None:
        settings = get_settings()
        if not should_run(
            settings, hands_free=hands_free, fallback_request=fallback_request
        ):
            return None
        started = time.time()
        packet = build_context_packet(
            utterance=text,
            requested_session=requested_session,
            trace_id=trace_id,
            hands_free=hands_free,
            settings=settings,
            context_scope="focused",
            fallback_request=fallback_request,
        )
        try:
            raw = self.model_call(packet, settings)
            decision = parse_decision(raw)
            if _should_scan_broader(decision, packet, requested_session):
                broad_packet = build_context_packet(
                    utterance=text,
                    requested_session=requested_session,
                    trace_id=trace_id,
                    hands_free=hands_free,
                    settings=settings,
                    context_scope="all",
                    fallback_request=fallback_request,
                )
                try:
                    raw = self.model_call(broad_packet, settings)
                    decision = parse_decision(raw)
                    packet = broad_packet
                except Exception as e:  # noqa: BLE001
                    log_exception("orchestratorBroadModelFail", e, detail=trace_id)
            latency_ms = int((time.time() - started) * 1000)
        except Exception as e:  # noqa: BLE001
            log_exception("orchestratorModelFail", e, detail=trace_id)
            decision = OrchestratorDecision(
                kind=DECISION_ERROR,
                reason="orchestrator model failed",
                raw={},
                error=str(e),
            )
            latency_ms = int((time.time() - started) * 1000)
        return self._apply_decision(
            decision=decision,
            packet=packet,
            settings=settings,
            requested_session=requested_session,
            trace_id=trace_id,
            prompt_admission=prompt_admission,
            hands_free=hands_free,
            synthesize_audio=synthesize_audio,
            unheard_audio_sessions=unheard_audio_sessions,
            latency_ms=latency_ms,
            dispatch=dispatch,
            fallback_request=fallback_request,
        )

    def transcribe_should_skip_herald(self, *, hands_free: bool) -> bool:
        settings = get_settings()
        return should_run(settings, hands_free=hands_free, fallback_request=False)

    def _apply_decision(
        self,
        *,
        decision: OrchestratorDecision,
        packet: dict[str, Any],
        settings: OrchestratorSettings,
        requested_session: str,
        trace_id: str,
        prompt_admission: PromptAdmission | None,
        hands_free: bool,
        synthesize_audio: bool,
        unheard_audio_sessions: tuple[str, ...],
        latency_ms: int,
        dispatch: Callable[..., Any],
        fallback_request: bool,
    ) -> OrchestratorOutcome:
        sessions = {a["session"] for a in packet.get("agents", [])}
        final_action = FINAL_ERROR
        fallback_used = False
        target = decision.target_session or requested_session
        error = decision.error
        dispatch_result = None

        try:
            if fallback_request and decision.kind not in {
                DECISION_AGENT_MESSAGE,
                DECISION_CORRECTION,
            }:
                # A failed-delegation fallback has exactly one authority: pick
                # a recipient. It must never turn the held utterance into a
                # status query, control command, or ignored dictation side effect.
                final_action = FINAL_CLARIFY
            if decision.kind in {DECISION_AGENT_MESSAGE, DECISION_CORRECTION}:
                target = self._valid_target(decision.target_session, sessions)
                if not target:
                    target = requested_session
                    final_action = FINAL_CLARIFY
                    decision = self._as_clarify(
                        decision,
                        "I am not sure which agent that was for.",
                    )
                    if not fallback_request:
                        self._hold_and_speak_clarification(
                            decision=decision,
                            packet=packet,
                            requested_session=requested_session,
                            trace_id=trace_id,
                            prompt_admission=prompt_admission,
                        )
                elif not self._route_confidently(decision, settings):
                    final_action = FINAL_CLARIFY
                    decision = self._as_clarify(
                        decision,
                        f"Was that for {self._persona_for_session(target) or target}?",
                    )
                    if not fallback_request:
                        self._hold_and_speak_clarification(
                            decision=decision,
                            packet=packet,
                            requested_session=requested_session,
                            trace_id=trace_id,
                            prompt_admission=prompt_admission,
                        )
                else:
                    held = self._claim_pending(decision.pending_id)
                    routed_admission = prompt_admission
                    if held is not None:
                        routed_admission = prompt_admissions.PromptAdmission.from_json(
                            str(held.get("prompt_admission_json") or "")
                        )
                    text_to_send = (
                        (held or {}).get("utterance")
                        or decision.text_to_send.strip()
                        or packet["utterance"]
                    )
                    dispatch_result = dispatch(
                        text=text_to_send,
                        requested_session=requested_session,
                        trace_id=trace_id,
                        client_msg_id=(
                            routed_admission.client_admission_id
                            if routed_admission else ""
                        ),
                        origin=routed_admission.origin if routed_admission else "user",
                        sender_agent_id=(
                            routed_admission.sender_agent_id
                            if routed_admission else ""
                        ),
                        prompt_admission=routed_admission,
                        synthesize_audio=synthesize_audio,
                        unheard_audio_sessions=unheard_audio_sessions,
                        forced_session=target,
                        routed_by_orchestrator=True,
                    )
                    record_routing_message(
                        session=target,
                        role="user",
                        text=text_to_send,
                        trace_id=trace_id,
                    )
                    final_action = FINAL_ROUTE
            if final_action == FINAL_ERROR and decision.kind in {
                DECISION_CLARIFY,
                DECISION_AMBIGUOUS,
            }:
                final_action = FINAL_CLARIFY
                if not fallback_request:
                    self._hold_and_speak_clarification(
                        decision=decision,
                        packet=packet,
                        requested_session=requested_session,
                        trace_id=trace_id,
                        prompt_admission=prompt_admission,
                    )
            if final_action == FINAL_ERROR and decision.kind == DECISION_STATUS:
                final_action = FINAL_STATUS
                target = self._valid_target(decision.target_session, sessions) or requested_session
                spoken = decision.status_text.strip() or self._status_text(target)
                self._speak_orchestrator(spoken, settings)
            if final_action == FINAL_ERROR and decision.kind == DECISION_CONTROL:
                final_action = FINAL_CONTROL
                self._run_control(decision, requested_session, settings)
            if final_action == FINAL_ERROR and decision.kind == DECISION_AGENT_CONTROL:
                final_action = FINAL_CONTROL
                target = self._run_agent_control(decision, requested_session)
            if final_action == FINAL_ERROR and decision.kind == DECISION_IGNORED:
                final_action = FINAL_IGNORED
                target = requested_session
            if final_action == FINAL_ERROR:
                fallback_used = True
                final_action = FINAL_FALLBACK
        except Exception as e:  # noqa: BLE001
            log_exception("orchestratorApplyFail", e, detail=trace_id)
            error = str(e)
            final_action = FINAL_ERROR

        decision_id = self._log_decision(
            decision=decision,
            packet=packet,
            settings=settings,
            requested_session=requested_session,
            trace_id=trace_id,
            hands_free=hands_free,
            latency_ms=latency_ms,
            target_session=target,
            final_action=final_action,
            fallback_used=fallback_used,
            error=error,
        )
        self.ctx.stream.broadcast({
            "type": "orchestrator-decision",
            "decision_id": decision_id,
            "trace_id": trace_id,
            "action": final_action,
            "kind": decision.kind,
            "target_session": target,
            "confidence": decision.confidence,
            "reason": decision.reason,
        })
        if dispatch_result is not None:
            return OrchestratorOutcome(
                handled=True,
                ok=True,
                session=dispatch_result.session,
                dispatch=dispatch_result.backend,
                trace_id=trace_id,
                action=final_action,
                decision_id=decision_id,
                decision=_public_decision(decision),
            )
        return OrchestratorOutcome(
            handled=True,
            ok=final_action != FINAL_ERROR,
            session=target,
            trace_id=trace_id,
            action=final_action,
            decision_id=decision_id,
            decision=_public_decision(decision),
            error=error,
            status=500 if final_action == FINAL_ERROR else 200,
        )

    def _route_confidently(
        self,
        decision: OrchestratorDecision,
        settings: OrchestratorSettings,
    ) -> bool:
        return decision.confidence >= settings.confidence_threshold

    def _as_clarify(self, decision: OrchestratorDecision, text: str) -> OrchestratorDecision:
        return OrchestratorDecision(
            **{
                **asdict(decision),
                "kind": DECISION_CLARIFY,
                "spoken_text": decision.spoken_text or text,
                "phrase_key": decision.phrase_key or "clarify_target",
            }
        )

    def _hold_and_speak_clarification(
        self,
        *,
        decision: OrchestratorDecision,
        packet: dict[str, Any],
        requested_session: str,
        trace_id: str,
        prompt_admission: PromptAdmission | None,
    ) -> None:
        pending_id = decision.pending_id or str(uuid.uuid4())
        target = decision.target_session or ""
        now = self.now()
        conn().execute(
            """INSERT INTO orchestrator_pending_utterances
                  (pending_id, trace_id, utterance, requested_session,
                   candidate_session, speak_as_session, reason, created_at,
                   expires_at, status, prompt_admission_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(pending_id) DO UPDATE SET
                   trace_id = excluded.trace_id,
                   utterance = excluded.utterance,
                   requested_session = excluded.requested_session,
                   candidate_session = excluded.candidate_session,
                   speak_as_session = excluded.speak_as_session,
                   reason = excluded.reason,
                   created_at = excluded.created_at,
                   expires_at = excluded.expires_at,
                   prompt_admission_json = excluded.prompt_admission_json,
                   status = 'pending'""",
            (
                pending_id,
                trace_id,
                packet["utterance"],
                requested_session,
                target,
                requested_session,
                decision.reason,
                now,
                now + PENDING_TTL_MS,
                prompt_admission.to_json() if prompt_admission else "",
            ),
        )
        spoken = decision.spoken_text.strip()
        if not spoken:
            if target:
                spoken = f"Was that for {self._persona_for_session(target) or target}?"
            else:
                spoken = "I am not sure who that was for."
        self._speak_as_session(spoken, requested_session, phrase_key=decision.phrase_key)

    def _claim_pending(self, pending_id: str) -> dict[str, Any] | None:
        if not pending_id:
            return None
        row = conn().execute(
            """SELECT pending_id, utterance, requested_session, candidate_session,
                      prompt_admission_json
                 FROM orchestrator_pending_utterances
                WHERE pending_id = ?
                  AND status = 'pending'
                  AND expires_at > ?""",
            (pending_id, self.now()),
        ).fetchone()
        if not row:
            return None
        conn().execute(
            """UPDATE orchestrator_pending_utterances
                  SET status = 'sent'
                WHERE pending_id = ?""",
            (pending_id,),
        )
        return dict(row)

    def _run_control(self, decision: OrchestratorDecision,
                     requested_session: str,
                     settings: OrchestratorSettings) -> None:
        action = (decision.control_action or "").strip().lower()
        target = decision.target_session or requested_session
        if action in {"repeat", "repeat_last", "say_again"}:
            if not self._replay_last_clip(target):
                self._speak_as_session(
                    "I do not have anything recent to replay.",
                    requested_session,
                    phrase_key="repeat_missing",
                )
            return
        if action in {"who_am_i_talking_to", "current_agent"}:
            persona = self._persona_for_session(requested_session) or requested_session
            self._speak_orchestrator(f"You are talking to {persona}.", settings)
            return
        self._speak_orchestrator("I am not sure what control action you meant.", settings)

    def _run_agent_control(self, decision: OrchestratorDecision,
                           requested_session: str) -> str:
        action = (decision.control_action or "").strip().lower()
        target = decision.target_session or requested_session
        if action in {"switch", "switch_focus"}:
            agent = agents_db.get_by_session(target)
            if agent:
                herald = getattr(self.ctx, "herald", None)
                with agents_db.focus_guard():
                    agents_db.set_focus(agent["agent_id"])
                    if herald is not None:
                        herald.set_focus(target)
                self.ctx.stream.broadcast({
                    "type": SSEType.AGENT_FOCUS,
                    "session": target,
                    "agent_id": agent["agent_id"],
                })
            return target
        if action in {"stop", "stop_agent", "interrupt"}:
            agent = agents_db.get_by_session(target)
            if agent:
                backends.interrupt_any(agent["agent_id"])
            return target
        if action in {"relaunch", "restart", "resume", "fork", "create"}:
            return self._lifecycle_action(decision, target)
        self._speak_as_session("I am not sure how to do that yet.", requested_session)
        return target

    def _lifecycle_action(self, decision: OrchestratorDecision, target: str) -> str:
        payload = dict(decision.control_payload or {})
        from .agent_lifecycle import AgentLifecycleService

        existing = agents_db.get_by_session(target) if target else None
        if existing and (decision.control_action or "").lower() in {
            "relaunch",
            "restart",
            "resume",
        }:
            payload.setdefault("name", existing["persona"])
            payload.setdefault("session", existing["session"])
            payload.setdefault("replace_sid", existing["session"])
            payload.setdefault("cwd", existing["cwd"])
            payload.setdefault("backend", existing["backend"])
        if "synthesize_audio" not in payload:
            payload["synthesize_audio"] = True
        result = AgentLifecycleService(self.ctx).create(payload)
        return result.session

    def _status_text(self, session: str) -> str:
        agent = agents_db.get_by_session(session)
        if not agent:
            return "I do not see that agent running."
        state = conn().execute(
            """SELECT kind, detail, ts FROM state_log
                WHERE agent_id = ?
                ORDER BY ts DESC, state_id DESC LIMIT 1""",
            (agent["agent_id"],),
        ).fetchone()
        messages = _recent_messages(agent["agent_id"], role="assistant", limit=2)
        persona = agent["persona"]
        if messages:
            snippet = messages[-1]["text"][:180]
            return f"{persona} was last working on: {snippet}"
        if state:
            return f"{persona} is currently {state['kind']}."
        return f"{persona} is active, but I do not have recent work details yet."

    def _speak_orchestrator(self, text: str, settings: OrchestratorSettings,
                            *, phrase_key: str = "") -> None:
        if phrase_key and self._play_cached_phrase(
            phrase_key=phrase_key,
            session="",
            voice_id=settings.voice_id,
        ):
            return
        self.ctx.speak_announcement(text, settings.voice_id, session=None)

    def _speak_as_session(self, text: str, session: str,
                          *, phrase_key: str = "") -> None:
        agent = agents_db.get_by_session(session)
        if agent:
            if phrase_key and self._play_cached_phrase(
                phrase_key=phrase_key,
                session=session,
                voice_id=agent.get("voice_id") or "",
            ):
                return
            self.ctx.speak_announcement(text, agent.get("voice_id"), session=session)

    def _play_cached_phrase(self, *, phrase_key: str, session: str,
                            voice_id: str) -> bool:
        row = conn().execute(
            """SELECT audio_path, text
                 FROM orchestrator_phrase_cache
                WHERE phrase_key = ?
                  AND voice_id = ?
                  AND COALESCE(session, '') = ?
                UNION ALL
               SELECT audio_path, text
                 FROM orchestrator_phrase_cache
                WHERE phrase_key = ?
                  AND voice_id = ?
                  AND COALESCE(session, '') = ''
                LIMIT 1""",
            (phrase_key, voice_id, session or "", phrase_key, voice_id),
        ).fetchone()
        if not row or not row["audio_path"]:
            return False
        path = str(row["audio_path"])
        if not path.startswith("/audio/") and not os.path.exists(path):
            return False
        url = path if path.startswith("/audio/") else f"/audio/{os.path.basename(path)}"
        agent = agents_db.get_by_session(session) if session else None
        self.ctx.stream.broadcast({
            "type": SSEType.AUDIO,
            "url": url,
            "session": session,
            "agent_id": (agent or {}).get("agent_id"),
            "voice_id": voice_id,
            "phrase_key": phrase_key,
            "cached_phrase": True,
        })
        return True

    def _replay_last_clip(self, session: str) -> bool:
        agent = agents_db.get_by_session(session)
        if not agent:
            return False
        row = conn().execute(
            """SELECT path, voice_id, trace_id, clip_id
                 FROM clips
                WHERE agent_id = ?
                ORDER BY created_at DESC, clip_id DESC
                LIMIT 1""",
            (agent["agent_id"],),
        ).fetchone()
        if not row:
            return False
        path = str(row["path"] or "")
        url = path if path.startswith("/audio/") else f"/audio/{os.path.basename(path)}"
        self.ctx.stream.broadcast({
            "type": SSEType.AUDIO,
            "url": url,
            "session": session,
            "agent_id": agent["agent_id"],
            "voice_id": row["voice_id"],
            "trace_id": row["trace_id"],
            "clip_id": row["clip_id"],
            "replay": True,
        })
        return True

    def _valid_target(self, session: str, sessions: set[str]) -> str:
        return session if session in sessions else ""

    def _persona_for_session(self, session: str) -> str:
        agent = agents_db.get_by_session(session)
        return str((agent or {}).get("persona") or "")

    def _log_decision(
        self,
        *,
        decision: OrchestratorDecision,
        packet: dict[str, Any],
        settings: OrchestratorSettings,
        requested_session: str,
        trace_id: str,
        hands_free: bool,
        latency_ms: int,
        target_session: str,
        final_action: str,
        fallback_used: bool,
        error: str,
    ) -> int:
        context = packet.get("context_summary", {})
        cur = conn().execute(
            """INSERT INTO orchestrator_decisions (
                   trace_id, utterance, requested_session, hands_free, enabled,
                   provider, model, effort, latency_ms, context_hash,
                   context_agent_count, context_message_count, decision_kind,
                   target_session, confidence, addressing, mentioned_sessions_json,
                   name_corrections_json, candidate_scores_json, reason,
                   raw_response_json, final_action, fallback_used, phrase_key,
                   error, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace_id,
                packet.get("utterance") or "",
                requested_session,
                1 if hands_free else 0,
                1 if settings.enabled else 0,
                settings.provider,
                settings.model,
                settings.effort,
                latency_ms,
                context.get("hash"),
                int(context.get("agent_count") or 0),
                int(context.get("message_count") or 0),
                decision.kind,
                target_session,
                float(decision.confidence or 0),
                1 if decision.addressing else 0,
                json.dumps(list(decision.mentioned_sessions), separators=(",", ":")),
                json.dumps(list(decision.name_corrections), separators=(",", ":")),
                json.dumps(list(decision.candidate_scores), separators=(",", ":")),
                decision.reason,
                json.dumps(decision.raw or {}, separators=(",", ":")),
                final_action,
                1 if fallback_used else 0,
                decision.phrase_key,
                error,
                self.now(),
            ),
        )
        decision_id = int(cur.lastrowid or 0)
        eventlog.emit(
            "orchestrator",
            "decision",
            trace_id=trace_id,
            session=target_session or requested_session,
            duration_ms=latency_ms,
            detail={
                "decision_id": decision_id,
                "kind": decision.kind,
                "final_action": final_action,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "fallback_used": fallback_used,
                "error": error,
            },
        )
        return decision_id


def build_context_packet(
    *,
    utterance: str,
    requested_session: str,
    trace_id: str,
    hands_free: bool,
    settings: OrchestratorSettings,
    context_scope: str = "all",
    fallback_request: bool = False,
) -> dict[str, Any]:
    focus_agent_id = agents_db.get_focus()
    focus = agents_db.get_by_agent_id(focus_agent_id) if focus_agent_id else None
    focus_session = (focus or {}).get("session") or requested_session
    include_all_context = context_scope == "all"
    detailed_sessions = {requested_session, focus_session}
    agents = []
    message_count = 0
    all_agents = [agent for agent in agents_db.list_agents()
                  if not agent.get("archived_at")]
    if fallback_request:
        cutoff = now_ms() - RECENT_AGENT_WINDOW_MS
        all_agents = [
            agent for agent in all_agents
            if agent["session"] in detailed_sessions
            or _agent_context_activity(agent) >= cutoff
        ]
    for agent in all_agents:
        agent_id = agent["agent_id"]
        include_detail = include_all_context or agent["session"] in detailed_sessions
        user_messages = (
            _recent_messages(agent_id, role="user", limit=RECENT_MESSAGES_PER_ROLE)
            if include_detail else []
        )
        assistant_messages = (
            _recent_messages(agent_id, role="assistant", limit=RECENT_MESSAGES_PER_ROLE)
            if include_detail else []
        )
        message_count += len(user_messages) + len(assistant_messages)
        state = _latest_state(agent_id) if include_detail else None
        agents.append({
            "session": agent["session"],
            "persona": agent["persona"],
            "voice_id": agent["voice_id"],
            "cwd": agent["cwd"],
            "backend": agent.get("backend"),
            "is_requested": agent["session"] == requested_session,
            "is_sticky_focus": bool(focus and focus["agent_id"] == agent_id),
            "state": _compact_state(state),
            "recent_user_messages": user_messages,
            "recent_agent_messages": assistant_messages,
        })
    pending = _pending_utterances(limit=3)
    candidates = _name_candidates(utterance, agents)
    stable = {
        "utterance": utterance,
        "requested_session": requested_session,
        "focus_session": focus_session,
        "context_scope": "all" if include_all_context else "focused",
        "fallback_request": fallback_request,
        "agents": agents,
        "pending": pending,
        "candidate_name_matches": candidates,
    }
    context_hash = hashlib.sha1(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        **stable,
        "trace_id": trace_id,
        "hands_free": hands_free,
        "settings": asdict(settings),
        "context_summary": {
            "hash": context_hash,
            "scope": "all" if include_all_context else "focused",
            "agent_count": len(agents),
            "message_count": message_count,
        },
        "routing_policy": {
            "wrong_agent_is_worse_than_clarification": True,
            "sticky_bias_session": focus_session,
            "high_confidence": HIGH_CONFIDENCE,
            "sticky_confidence": STICKY_CONFIDENCE,
            "clarify_confidence": CLARIFY_CONFIDENCE,
            "automatic_confidence_threshold": settings.confidence_threshold,
            "recent_agent_window_minutes": 30 if fallback_request else None,
        },
    }


def _agent_context_activity(agent: dict[str, Any]) -> int:
    latest_state = _latest_state(agent["agent_id"])
    return max(
        int(agent.get("created_at") or 0),
        int(agents_db.last_activity(agent["agent_id"]) or 0),
        int(latest_state["ts"] or 0) if latest_state else 0,
    )


def _recent_messages(agent_id: str, *, role: str, limit: int) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT role, text, trace_id, source, created_at
             FROM (
                   SELECT role, text, trace_id, source, created_at
                     FROM agent_routing_messages
                    WHERE agent_id = ? AND role = ?
                   UNION ALL
                   SELECT role, text, '' AS trace_id, 'transcript' AS source, updated_at AS created_at
                     FROM messages
                    WHERE agent_id = ? AND role = ?
             )
            ORDER BY created_at DESC
            LIMIT ?""",
        (agent_id, role, agent_id, role, max(1, limit)),
    ).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for item in out:
        item["text"] = _compact_text(item.get("text"), MESSAGE_CONTEXT_CHARS)
    return out


def _latest_state(agent_id: str):
    return conn().execute(
        """SELECT kind, detail, ts FROM state_log
            WHERE agent_id = ?
            ORDER BY ts DESC, state_id DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()


def _compact_state(row) -> dict[str, Any] | None:
    if not row:
        return None
    state = dict(row)
    state["detail"] = _compact_text(state.get("detail"), STATE_DETAIL_CONTEXT_CHARS)
    return state


def _compact_text(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _pending_utterances(limit: int) -> list[dict[str, Any]]:
    now = now_ms()
    rows = conn().execute(
        """SELECT pending_id, trace_id, utterance, requested_session,
                  candidate_session, speak_as_session, reason, created_at,
                  expires_at
             FROM orchestrator_pending_utterances
            WHERE status = 'pending' AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT ?""",
        (now, max(1, limit)),
    ).fetchall()
    out = [dict(r) for r in rows]
    for item in out:
        item["utterance"] = _compact_text(item.get("utterance"), PENDING_CONTEXT_CHARS)
        item["reason"] = _compact_text(item.get("reason"), PENDING_CONTEXT_CHARS)
    return out


def _name_candidates(utterance: str, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = re.findall(r"[A-Za-z0-9]+", utterance.lower())[:12]
    out = []
    for agent in agents:
        names = {agent["session"].lower(), agent["persona"].lower()}
        best = 0.0
        matched = ""
        for token in tokens:
            for name in names:
                score = difflib.SequenceMatcher(None, token, name).ratio()
                if score > best:
                    best = score
                    matched = token
        if best >= 0.62:
            out.append({
                "session": agent["session"],
                "persona": agent["persona"],
                "matched_token": matched,
                "score": round(best, 3),
            })
    return sorted(out, key=lambda x: x["score"], reverse=True)


def call_model(packet: dict[str, Any], settings: OrchestratorSettings) -> dict[str, Any]:
    """Ask the configured provider for one routing decision.

    OpenAI goes straight to the API. Every other provider is a registry
    backend whose ``routing_module`` builds the one-shot argv and extracts
    the reply text, so a new CLI can route the moment its adapter says so.
    """
    provider = normalize_provider(settings.provider)
    prompt = _model_prompt(packet)
    if provider == OPENAI_PROVIDER:
        return _call_openai(prompt, settings)
    adapter = backends.get(provider)
    if adapter is None or not adapter.supports_routing:
        raise RuntimeError(f"unsupported orchestrator provider: {settings.provider}")
    runner = importlib.import_module(f"lib.{adapter.routing_module}")
    cmd = runner.routing_cmd(prompt, model=settings.model, effort=settings.effort)
    binary = cmd[0]
    if shutil.which(binary) is None:
        raise FileNotFoundError(f"{binary} is not on PATH")
    from .launch_paths import existing_workspace_path
    proc = subprocess.run(
        cmd,
        cwd=str(existing_workspace_path(None)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(0.25, settings.timeout_ms / 1000.0),
        env={**os.environ},
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{binary} rc={proc.returncode}")[:1000])
    return _extract_json(runner.routing_text(proc.stdout or ""))


_OPENAI_EFFORTS = set(OPENAI_EFFORTS)


def _call_openai(prompt: str, settings: OrchestratorSettings) -> dict[str, Any]:
    """Route via the OpenAI Chat Completions API (e.g. gpt-5.4-nano).

    Far faster than spawning the `claude`/`agy` CLI (~1s vs ~20-30s) because
    there's no agent cold-start. JSON mode + a reasoning model at low effort.
    """
    import urllib.error
    import urllib.request

    model = (settings.model or "gpt-5.4-nano").strip()
    effort = settings.effort.strip().lower()
    if not effort:
        effort = DEFAULT_EFFORT
    elif effort not in _OPENAI_EFFORTS:
        raise RuntimeError(f"unsupported OpenAI reasoning effort: {settings.effort}")
    key = config.load().openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured ([openai] api_key or env)")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": effort,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout = max(0.25, settings.timeout_ms / 1000.0)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500] if e.fp else str(e)
        raise RuntimeError(f"openai http {e.code}: {detail}") from e
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected openai response: {str(data)[:300]}") from e
    return _extract_json(content or "")


def _model_prompt(packet: dict[str, Any]) -> str:
    fallback_instruction = ""
    if packet.get("fallback_request"):
        fallback_instruction = (
            "This request is a failed deterministic delegation: the client already "
            "tried its configured name-matching rule and found no single recipient. "
            "Your only job is to infer which recent agent should receive the message. "
            "Use recent conversation fit, not merely name mentions. Return agent_message "
            "with the best target_session and an honest confidence, or ambiguous/clarify "
            "when the evidence is insufficient. Never return status or control actions. "
            "Only agents included in INPUT are eligible; they were active in the last "
            "30 minutes, apart from the requested/focused session retained for context.\n"
        )
    return (
        "You are the low-latency hands-free routing orchestrator for a local "
        "multi-agent voice app. Return JSON only. Do not speak to the user.\n"
        f"{fallback_instruction}"
        "Your job is to decide whether the utterance addresses an agent, merely "
        "mentions an agent, asks status, requests a control action, should be "
        "ignored as accidental dictation/noise, or needs clarification. A name "
        "mention is not enough to route: distinguish "
        "addressing from referring. Account for STT mistakes, for example Mark "
        "may mean Mike only if the context supports it. Wrong-agent routing is "
        "worse than asking a short clarification. Bias toward the sticky focus "
        "only when context is compatible. Where two agents remain genuinely "
        "indistinguishable after weighing the content - and only then, as a "
        "last resort rather than a shortcut - lean slightly toward the one "
        "most recently spoken to. This is a tiebreak, not a preference: it "
        "must never outweigh what the utterance is actually about. "
        "In active hands-free dictation, if the "
        "utterance is complete nonsense, accidental background speech, an "
        "unrelated fragment, or language/content that has no plausible relation "
        "to any active agent, return ignored instead of routing or clarifying.\n"
        "Valid kind values: agent_message, status_query, control, agent_control, "
        "recipient_correction, clarify, ambiguous, ignored, error.\n"
        "For agent_message include target_session, confidence 0..1, addressing, "
        "text_to_send, reason. For mention-only status questions use status_query "
        "and do not route to the agent. For repeat/say-that-again use control "
        "with control_action repeat_last. For relaunch/resume/fork/stop/switch "
        "use agent_control with a control_action and target_session. For ignored, "
        "include confidence and a short reason; do not include spoken_text. If "
        "unsure whether it is accidental or meaningful, return clarify or "
        "ambiguous with spoken_text.\n"
        f"INPUT:\n{json.dumps(packet, separators=(',', ':'), ensure_ascii=True)}"
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("empty model response")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("orchestrator response must be a JSON object")
    return data


def parse_decision(raw: dict[str, Any]) -> OrchestratorDecision:
    kind = str(raw.get("kind") or raw.get("decision_kind") or "").strip()
    if kind not in {
        DECISION_AGENT_MESSAGE,
        DECISION_CONTROL,
        DECISION_STATUS,
        DECISION_AGENT_CONTROL,
        DECISION_CORRECTION,
        DECISION_CLARIFY,
        DECISION_AMBIGUOUS,
        DECISION_IGNORED,
        DECISION_ERROR,
    }:
        kind = DECISION_ERROR
    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    mentioned_raw = raw.get("mentioned_sessions") or []
    mentioned = tuple(str(x) for x in mentioned_raw) if isinstance(mentioned_raw, list) else ()
    corrections = raw.get("name_corrections") or []
    candidates = raw.get("candidate_scores") or []
    return OrchestratorDecision(
        kind=kind,
        target_session=str(raw.get("target_session") or "").strip(),
        confidence=confidence,
        addressing=bool(raw.get("addressing")),
        mentioned_sessions=mentioned,
        name_corrections=tuple(corrections if isinstance(corrections, list) else []),
        candidate_scores=tuple(candidates if isinstance(candidates, list) else []),
        text_to_send=str(raw.get("text_to_send") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
        phrase_key=str(raw.get("phrase_key") or "").strip(),
        spoken_text=str(raw.get("spoken_text") or "").strip(),
        pending_id=str(raw.get("pending_id") or "").strip(),
        control_action=str(raw.get("control_action") or "").strip(),
        control_payload=raw.get("control_payload") if isinstance(raw.get("control_payload"), dict) else None,
        status_text=str(raw.get("status_text") or "").strip(),
        raw=raw,
        error=str(raw.get("error") or "").strip(),
    )


def _public_decision(decision: OrchestratorDecision) -> dict[str, Any]:
    return {
        "kind": decision.kind,
        "target_session": decision.target_session,
        "confidence": decision.confidence,
        "addressing": decision.addressing,
        "reason": decision.reason,
        "phrase_key": decision.phrase_key,
        "control_action": decision.control_action,
    }
