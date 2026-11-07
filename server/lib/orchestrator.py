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
from . import events, identity
from . import backends, config, eventlog, janitor_builtins, janitors, settings_store
from .db import conn, now_ms
from .log import log_exception
from .prompt_admissions import PromptAdmission
from .turn_dispatch import DispatchError
from . import prompt_admissions


from .orchestrator_types import (  # noqa: E402,F401  (split_apply: orchestrator)
    DEFAULT_PROVIDER, JANITOR_ROLE, POLICY_OPTION_KEYS, DEFAULT_MODEL, DEFAULT_EFFORT,
    OPENAI_PROVIDER, TYPESAFE_PROVIDER, OPENAI_CATALOG_BACKEND, OPENAI_EFFORTS, DEFAULT_TIMEOUT_MS,
    ORCHESTRATOR_VOICE_ID, KEY_ENABLED, KEY_HANDS_FREE_ONLY, KEY_FALLBACK_ONLY,
    KEY_CONFIDENCE_THRESHOLD, KEY_PROVIDER, KEY_MODEL, KEY_EFFORT, KEY_TIMEOUT_MS, KEY_VOICE_ID,
    HIGH_CONFIDENCE, STICKY_CONFIDENCE, CLARIFY_CONFIDENCE, PENDING_TTL_MS,
    RECENT_MESSAGES_PER_ROLE, MESSAGE_CONTEXT_CHARS, STATE_DETAIL_CONTEXT_CHARS,
    PENDING_CONTEXT_CHARS, RECENT_AGENT_WINDOW_MS, DECISION_AGENT_MESSAGE, DECISION_CONTROL,
    DECISION_STATUS, DECISION_AGENT_CONTROL, DECISION_CORRECTION, DECISION_CLARIFY,
    DECISION_AMBIGUOUS, DECISION_IGNORED, DECISION_ERROR, FINAL_ROUTE, FINAL_CLARIFY, FINAL_STATUS,
    FINAL_CONTROL, FINAL_IGNORED, FINAL_FALLBACK, FINAL_ERROR, OrchestratorSettings,
    OrchestratorDecision, OrchestratorOutcome, DISPATCH_RETRYABLE, TYPESAFE_TARGET_MIN,
    TYPESAFE_DECISION_MIN,
)
from .orchestrator_model import (  # noqa: E402,F401  (split_apply: orchestrator)
    _OPENAI_ALIASES, _JSON_RE, normalize_provider, call_model, _OPENAI_EFFORTS, _call_openai,
    _model_prompt, _typesafe_questions, _typesafe_state, _call_typesafe, _extract_json,
    parse_decision, _public_decision,
)
class _StaleJanitorRun(RuntimeError):
    pass


def provider_options() -> list[dict[str, Any]]:
    """Routing providers this Host can run: catalogue backends plus OpenAI.

    Served on /orchestrator/settings so the clients' provider dropdown is the
    registry, not a bundled enum. ``installed`` is a cheap PATH check, not the
    full capability probe; ``effort_options`` null means "derive from the
    /agent-model-options row named by catalog_backend".
    """
    rows: list[dict[str, Any]] = []
    for adapter in backends.routing_adapters():
        binary = backends.by_id(adapter.id).executable()
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
        "id": TYPESAFE_PROVIDER,
        "label": "TypeSafe Jev",
        "detail": ("Typed decision model: picks the action and the agent in one "
                   "call. Falls back to OpenAI when it needs to compose a "
                   "clarifying question."),
        "kind": "api",
        "catalog_backend": "",
        "installed": bool(config.load().typesafe_key()),
        "effort_options": [],
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
    if provider in (OPENAI_PROVIDER, TYPESAFE_PROVIDER):
        return True
    adapter = backends.get(provider)
    return adapter is not None and adapter.supports_routing


def addressing_status() -> dict:
    """Current mode plus the catalogue, so the app can draw the picker."""
    from . import addressing
    return {"mode": addressing.mode(), "modes": list(addressing.MODES)}


def get_legacy_settings() -> OrchestratorSettings:
    """Read the pre-Janitor settings once for migration; never overwrite an owner."""
    cfg = config.load()
    provider_default = DEFAULT_PROVIDER
    provider = settings_store.get_text(KEY_PROVIDER, default=provider_default).strip()
    provider = provider or provider_default
    canonical = normalize_provider(provider)
    adapter = backends.get(canonical)
    if canonical == OPENAI_PROVIDER:
        model_default = DEFAULT_MODEL
    elif adapter is not None and adapter.model_carries_effort:
        # The effort lives in the model id, so the default must name a model.
        model_default = backends.default_model_effort(canonical, cfg)[0]
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


def _settings_from_configuration(value: dict, *, enabled: bool, provider: str) -> OrchestratorSettings:
    options = janitors.option_values(JANITOR_ROLE, value.get("options", {}))
    return OrchestratorSettings(
        enabled=enabled, provider=provider, model=value["model"], effort=value["effort"],
        **{key: options[key] for key in POLICY_OPTION_KEYS},
    )


def get_settings() -> OrchestratorSettings:
    """Legacy HTTP shape backed entirely by the installed Janitor configuration."""
    owner = janitor_builtins.get_builtin(JANITOR_ROLE)
    if owner is None:
        return get_legacy_settings()
    return _settings_from_configuration(
        owner, enabled=bool(owner["enabled"]), provider=owner["execution"]["provider"],
    )


def _ensure_janitor() -> None:
    if janitor_builtins.get_builtin(JANITOR_ROLE) is not None:
        return
    legacy = get_legacy_settings()
    provider = normalize_provider(legacy.provider)
    janitor_builtins.ensure_builtins(initial={JANITOR_ROLE: {
        "enabled": legacy.enabled,
        "backend": OPENAI_CATALOG_BACKEND if provider == OPENAI_PROVIDER else provider,
        "provider": provider, "model": legacy.model, "effort": legacy.effort,
        "options": {key: getattr(legacy, key) for key in POLICY_OPTION_KEYS},
    }})


def update_settings(data: dict[str, Any]) -> OrchestratorSettings:
    """Compatibility writes update the same Janitor settings as its native editor."""
    supported = {"enabled", "provider", "model", "effort", *POLICY_OPTION_KEYS}
    if not isinstance(data, dict) or data.keys() - supported:
        raise ValueError("Unsupported orchestrator settings")
    if not data:
        return get_settings()
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ValueError("Enabled must be a boolean")
    provider = None
    if "provider" in data:
        provider = normalize_provider(str(data.get("provider") or DEFAULT_PROVIDER))
        if not is_routing_provider(provider):
            raise ValueError(f"unsupported orchestrator provider: {provider}")
    _ensure_janitor()
    owner = janitor_builtins.get_builtin(JANITOR_ROLE)
    if owner is None:
        raise ValueError("The message delegator Janitor is unavailable")
    enabled = data.get("enabled", owner["enabled"])
    changes = {key: str(data.get(key) or "").strip()
               for key in ("model", "effort") if key in data}
    options = {key: data[key] for key in POLICY_OPTION_KEYS if key in data}
    if options:
        changes["options"] = options
    if provider is not None:
        if provider != owner["execution"]["provider"]:
            # A provider-only legacy request selects that provider's defaults;
            # a model from another catalogue may be invalid.
            changes.setdefault("model", DEFAULT_MODEL if provider == OPENAI_PROVIDER else "")
            changes.setdefault("effort", DEFAULT_EFFORT if provider == OPENAI_PROVIDER else "")
        changes.update(execution={"executor": "ephemeral", "provider": provider},
                       backend=OPENAI_CATALOG_BACKEND if provider == OPENAI_PROVIDER else provider)
    if changes:
        owner = janitors.configure(owner["session"], owner["revision"], **changes)
    if enabled != owner["enabled"]:
        janitors.set_enabled(owner["session"], owner["revision"], enabled)
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
                           trace_id: str = "", source: str = "orchestrator",
                           once: bool = False) -> None:
    """``once``: only if the session has no row of this role under this
    trace yet, decided by the INSERT itself so concurrent callers add one
    (RTO-07, a retried routed dispatch)."""
    agent = identity.lookup(session)
    if not agent or not text.strip():
        return
    conn().execute(
        """INSERT INTO agent_routing_messages
              (agent_id, session, role, text, trace_id, source, created_at)
           SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE NOT ? OR NOT EXISTS (
                SELECT 1 FROM agent_routing_messages
                 WHERE session = ? AND role = ? AND trace_id = ?)""",
        (
            agent["agent_id"],
            session,
            role,
            text.strip(),
            trace_id,
            source,
            now_ms(),
            once,
            session,
            role,
            trace_id,
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
        _ensure_janitor()

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
        only_sessions: tuple[str, ...] | None = None,
    ) -> OrchestratorOutcome | None:
        client_id = (prompt_admissions.message_id(prompt_admission.client_admission_id)
                     if prompt_admission else trace_id)
        request_id = "routing-" + hashlib.sha256(client_id.encode()).hexdigest()
        target_agent = identity.lookup(requested_session)
        target_agent_id = (target_agent or {}).get("agent_id")
        request_hash = hashlib.sha256(json.dumps({
            "utterance": text, "requested_session": requested_session,
            "hands_free": hands_free, "fallback_request": fallback_request,
            "origin": prompt_admission.origin if prompt_admission else "user",
            "sender_agent_id": prompt_admission.sender_agent_id if prompt_admission else "",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prior = janitor_builtins.get_request_run(JANITOR_ROLE, request_id)
        if prior is not None:
            frozen = prior["configuration"]
            if (frozen.get("context") != {"input_hash": request_hash}
                    or frozen.get("target_agent_id") != target_agent_id):
                return OrchestratorOutcome(
                    handled=True, ok=False, session=requested_session,
                    trace_id=trace_id, action=FINAL_ERROR, status=409,
                    error="Request identity already refers to different work",
                )
            receipt = prior.get("demand_result") or {}
            if receipt.get("reason") == DISPATCH_RETRYABLE:
                return self._retry_routed_dispatch(
                    receipt, requested_session=requested_session, trace_id=trace_id,
                    prompt_admission=prompt_admission, synthesize_audio=synthesize_audio,
                    unheard_audio_sessions=unheard_audio_sessions, dispatch=dispatch)
            return self._replayed_run(prior["run_id"], requested_session, trace_id)
        owner = janitor_builtins.resolve(JANITOR_ROLE, target_agent_id=target_agent_id)
        if owner is None:
            return None
        settings = _settings_from_configuration(
            owner, enabled=owner["enabled"], provider=owner["execution"]["provider"],
        )
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
            only_sessions=only_sessions,
        )
        try:
            run = janitor_builtins.begin_run(
                JANITOR_ROLE, request_id, context={"input_hash": request_hash},
                target_agent_id=target_agent_id,
            )
        except janitors.JanitorError as exc:
            # Reusing a request ID with different content is an admission
            # conflict, never a reason to dispatch that content by fallback.
            return OrchestratorOutcome(
                handled=True, ok=False, session=requested_session,
                trace_id=trace_id, action=FINAL_ERROR, error=str(exc), status=exc.status,
            )
        if run is None:
            return self._unavailable(packet, settings, requested_session, trace_id,
                                     hands_free, "Message delegator is busy or paused")
        run_id = run["run_id"]
        if not janitor_builtins.claim_run(run_id):
            return self._replayed_run(run_id, requested_session, trace_id)
        frozen = run["configuration"]
        settings = _settings_from_configuration(frozen, enabled=True, provider=frozen["provider"])
        packet["settings"] = asdict(settings)
        packet["routing_policy"]["automatic_confidence_threshold"] = settings.confidence_threshold
        if not should_run(settings, hands_free=hands_free, fallback_request=fallback_request):
            janitor_builtins.complete_run(run_id, outcome="cancelled", result={
                "summary": "Routing policy changed before execution", "status": FINAL_FALLBACK,
            })
            return self._unavailable(packet, settings, requested_session, trace_id,
                                     hands_free, "Message delegator policy changed")
        try:
            self._require_current(run_id)
            decision = self._call_with_fallback(packet, settings, run_id, "initial")
            self._require_current(run_id)
            if _should_scan_broader(decision, packet, requested_session):
                broad_packet = build_context_packet(
                    utterance=text,
                    requested_session=requested_session,
                    trace_id=trace_id,
                    hands_free=hands_free,
                    settings=settings,
                    context_scope="all",
                    fallback_request=fallback_request,
                    only_sessions=only_sessions,
                )
                try:
                    self._require_current(run_id)
                    decision = self._call_with_fallback(broad_packet, settings, run_id, "broad")
                    packet = broad_packet
                except _StaleJanitorRun:
                    raise
                except Exception as e:  # noqa: BLE001
                    log_exception("orchestratorBroadModelFail", e, detail=trace_id)
            latency_ms = int((time.time() - started) * 1000)
        except _StaleJanitorRun:
            janitor_builtins.complete_run(run_id, outcome="cancelled")
            return self._unavailable(packet, settings, requested_session, trace_id,
                                     hands_free, "Message delegator configuration changed")
        except Exception as e:  # noqa: BLE001
            log_exception("orchestratorModelFail", e, detail=trace_id)
            decision = OrchestratorDecision(
                kind=DECISION_ERROR,
                reason="orchestrator model failed",
                raw={},
                error=str(e),
            )
            latency_ms = int((time.time() - started) * 1000)
        result = self._apply_decision(
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
            janitor_run_id=run_id,
        )
        recipient = identity.lookup(result.session)
        failed = decision.kind == DECISION_ERROR or result.action == FINAL_ERROR
        receipt = {"summary": f"Message routing finished: {result.action}",
                   "status": result.action,
                   "target_agent_id": (recipient or {}).get("agent_id", "")}
        if result.action == FINAL_ERROR and result.status != 500:
            receipt["http_status"] = result.status
        if result.retry_dispatch:
            # The decision stands; a same-id retry sends it again (RTO-07).
            receipt.update(reason=DISPATCH_RETRYABLE, decision_id=result.decision_id)
        janitor_builtins.complete_run(
            run_id, outcome="failed" if failed else "completed",
            result=receipt,
            error="Message routing failed" if failed else "",
        )
        return result

    def _retry_routed_dispatch(self, receipt, *, requested_session, trace_id,
                               prompt_admission, synthesize_audio,
                               unheard_audio_sessions, dispatch) -> OrchestratorOutcome:
        """Send a routed message again whose dispatch failed transiently
        (RTO-07): the recorded decision, recipient and text under the same
        client message id, with no second model call. turn_dispatch launches
        a saved message that never started once and answers a started one
        without a second turn, so every later retry is safe."""
        from .turn_dispatch import DispatchError
        recipient = identity.lookup(receipt.get("target_agent_id")) or {}
        row = conn().execute(
            "SELECT utterance, raw_response_json, trace_id FROM orchestrator_decisions"
            " WHERE decision_id = ?",
            (receipt.get("decision_id"),),
        ).fetchone()
        if not recipient.get("session") or row is None:
            return OrchestratorOutcome(
                handled=True, ok=False, session=requested_session, trace_id=trace_id,
                action=FINAL_ERROR, error="Message routing failed", status=500)
        raw = json.loads(row["raw_response_json"] or "{}")
        held = self._held_utterance(str(raw.get("pending_id") or ""))
        routed_admission = prompt_admission
        if held is not None:
            routed_admission = PromptAdmission.from_json(str(held.get("prompt_admission_json") or ""))
        text_to_send = ((held or {}).get("utterance")
                        or str(raw.get("text_to_send") or "").strip()
                        or row["utterance"])
        try:
            result = dispatch(
                text=text_to_send,
                requested_session=requested_session,
                trace_id=trace_id,
                client_msg_id=routed_admission.client_admission_id if routed_admission else "",
                origin=routed_admission.origin if routed_admission else "user",
                sender_agent_id=routed_admission.sender_agent_id if routed_admission else "",
                prompt_admission=routed_admission,
                synthesize_audio=synthesize_audio,
                unheard_audio_sessions=unheard_audio_sessions,
                forced_session=recipient["session"],
                routed_by_orchestrator=True,
            )
        except DispatchError as e:
            log_exception("orchestratorRetryDispatchFail", e, detail=trace_id)
            return OrchestratorOutcome(
                handled=True, ok=False, session=recipient["session"], trace_id=trace_id,
                action=FINAL_ERROR, error=str(e), status=e.status)
        # Routing context gets the message once, under the routed request's
        # trace: a later same-id retry is answered by turn_dispatch's dedup
        # and must not add it again (lead, RTO-07 amendment).
        routed_trace = row["trace_id"] or trace_id
        record_routing_message(session=recipient["session"], role="user",
                               text=text_to_send, trace_id=routed_trace, once=True)
        return OrchestratorOutcome(
            handled=True, ok=True, session=result.session, dispatch=result.backend,
            trace_id=trace_id, action=FINAL_ROUTE, decision_id=receipt.get("decision_id"))

    def _call_with_fallback(self, packet, settings, run_id, phase):
        from dataclasses import replace
        from . import model_fallbacks
        run = janitors.get_run(run_id)
        primary = {"backend": settings.provider, "model": settings.model, "effort": settings.effort}
        effective=run.get("configuration",{}).get("effective_chain",{})
        if effective.get("source")=="global" and effective.get("chain"):
            chosen=effective["chain"][0]
            primary={"backend":chosen["provider"],"model":chosen["model"],"effort":""}
        def invoke(model):
            selected = replace(settings, provider=model["backend"], model=model["model"], effort=model.get("effort", ""))
            return parse_decision(self.model_call(packet, selected))
        return model_fallbacks.execute(run["agent_id"], run_id + ":" + phase, primary, invoke,
            current=lambda: janitor_builtins.is_current(run_id))

    @staticmethod
    def _require_current(run_id: str) -> None:
        if run_id and not janitor_builtins.is_current(run_id):
            raise _StaleJanitorRun("Message delegator configuration changed")

    def _unavailable(self, packet, settings, requested_session, trace_id,
                     hands_free, reason) -> OrchestratorOutcome:
        decision = OrchestratorDecision(kind=DECISION_ERROR, reason=reason)
        decision_id = self._log_decision(
            decision=decision, packet=packet, settings=settings,
            requested_session=requested_session, trace_id=trace_id,
            hands_free=hands_free, latency_ms=0, target_session=requested_session,
            final_action=FINAL_FALLBACK, fallback_used=True, error="",
        )
        return OrchestratorOutcome(
            handled=True, ok=True, session=requested_session, trace_id=trace_id,
            action=FINAL_FALLBACK, decision_id=decision_id,
            decision=_public_decision(decision),
        )

    @staticmethod
    def _replayed_run(run_id, requested_session, trace_id) -> OrchestratorOutcome:
        """A durable execution claim survives retries and Host restarts."""
        run = janitors.get_run(run_id) or {}
        if run.get("status") in {"queued", "running"}:
            # Retries bypass begin_run, which normally retires expired claims.
            # Reconcile here too so an interrupted request cannot stay pending
            # forever merely because every retry uses its original client ID.
            janitor_builtins.recover_expired_runs()
            run = janitors.get_run(run_id) or {}
        receipt = run.get("demand_result") or {}
        if run.get("status") == "cancelled" and not receipt:
            return OrchestratorOutcome(
                handled=True, ok=False, session=requested_session,
                trace_id=trace_id, action="cancelled", status=409,
                error=run.get("error") or "Message routing was cancelled before completion",
            )
        recipient = identity.lookup(receipt.get("target_agent_id")) or {}
        pending = run.get("status") in {"queued", "running"}
        action = "pending" if pending else receipt.get("status", FINAL_CLARIFY)
        return OrchestratorOutcome(
            handled=True, ok=action != FINAL_ERROR,
            session=recipient.get("session", requested_session),
            dispatch=recipient.get("backend", "") if action == FINAL_ROUTE else "",
            trace_id=trace_id, action=action,
            error="Message routing failed" if action == FINAL_ERROR else "",
            status=(202 if pending
                    else int(receipt.get("http_status") or 500) if action == FINAL_ERROR
                    else 200),
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
        janitor_run_id: str = "",
    ) -> OrchestratorOutcome:
        sessions = {a["session"] for a in packet.get("agents", [])}
        final_action = FINAL_ERROR
        fallback_used = False
        target = decision.target_session or requested_session
        error = decision.error
        dispatch_result = None
        dispatch_status = 0

        try:
            self._require_current(janitor_run_id)
            control_target = identity.lookup(decision.target_session)
            if (decision.kind in {DECISION_CONTROL, DECISION_AGENT_CONTROL}
                    and control_target is not None
                    and not agents_db.interaction_capabilities(control_target)["can_voice_target"]):
                decision = self._as_clarify(decision, "That agent does not accept voice commands.")
            # The agent the action will act on, the requested session when the
            # model names none; create acts on no existing agent.
            action = (decision.control_action or "").strip().lower()
            acted_on = (None if decision.kind == DECISION_AGENT_CONTROL and action == "create"
                        else identity.lookup(decision.target_session or requested_session))
            if (decision.kind in {DECISION_CONTROL, DECISION_AGENT_CONTROL}
                    and (not self._route_confidently(decision, settings)
                         or (acted_on is not None and acted_on["session"] not in sessions))):
                # A command clears the bar a message does and acts only on the
                # agents of this packet, a group call's participants (RTO-03).
                decision = self._as_clarify(decision, "I am not sure what you wanted me to do.")
            elif (decision.kind == DECISION_AGENT_CONTROL
                    and action == "fork"):
                # No fork source is trusted from the router (RTO-03).
                decision = self._as_clarify(decision, "Please fork agents from the app.")
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
                    self._require_current(janitor_run_id)
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
                    self._require_current(janitor_run_id)
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
                self._require_current(janitor_run_id)
                final_action = FINAL_STATUS
                target = self._valid_target(decision.target_session, sessions) or requested_session
                spoken = decision.status_text.strip() or self._status_text(target)
                self._speak_orchestrator(spoken, settings)
            if final_action == FINAL_ERROR and decision.kind == DECISION_CONTROL:
                self._require_current(janitor_run_id)
                final_action = FINAL_CONTROL
                self._run_control(decision, requested_session, settings)
            if final_action == FINAL_ERROR and decision.kind == DECISION_AGENT_CONTROL:
                self._require_current(janitor_run_id)
                final_action = FINAL_CONTROL
                target = self._run_agent_control(decision, requested_session)
            if final_action == FINAL_ERROR and decision.kind == DECISION_IGNORED:
                final_action = FINAL_IGNORED
                target = requested_session
            if final_action == FINAL_ERROR:
                fallback_used = True
                final_action = FINAL_FALLBACK
        except _StaleJanitorRun as e:
            decision = OrchestratorDecision(kind=DECISION_ERROR, reason=str(e))
            target = requested_session
            fallback_used = True
            final_action = FINAL_FALLBACK
        except DispatchError as e:
            # The dispatcher's own answer (RTO-07): a 5xx is transient and the
            # client's same-id retry sends the routed message again; a 4xx
            # stands.
            log_exception("orchestratorDispatchFail", e, detail=trace_id)
            error = str(e)
            final_action = FINAL_ERROR
            dispatch_status = e.status
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
        events.broadcast(self.ctx.stream, events.orchestrator_decision(
            decision_id=decision_id, trace_id=trace_id, action=final_action,
            kind=decision.kind, target_session=target,
            confidence=decision.confidence, reason=decision.reason))
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
            status=(dispatch_status or 500) if final_action == FINAL_ERROR else 200,
            retry_dispatch=dispatch_status >= 500,
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

    @staticmethod
    def _held_utterance(pending_id: str) -> dict[str, Any] | None:
        """A held utterance a route already claimed, read again for its retry."""
        if not pending_id:
            return None
        row = conn().execute(
            """SELECT utterance, prompt_admission_json FROM orchestrator_pending_utterances
                WHERE pending_id = ?""",
            (pending_id,),
        ).fetchone()
        return dict(row) if row else None

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
            agent = identity.lookup(target)
            if agent:
                herald = getattr(self.ctx, "herald", None)
                with agents_db.focus_guard():
                    agents_db.set_focus(agent["agent_id"])
                    if herald is not None:
                        herald.set_focus(target)
                events.broadcast(self.ctx.stream, events.agent_focus(
                    session=target, agent_id=agent["agent_id"]))
            return target
        if action in {"stop", "stop_agent", "interrupt"}:
            agent = identity.lookup(target)
            if agent:
                backends.interrupt_any(agent["agent_id"])
            return target
        if action in {"relaunch", "restart", "resume", "create"}:
            return self._lifecycle_action(decision, target)
        self._speak_as_session("I am not sure how to do that yet.", requested_session)
        return target

    def _lifecycle_action(self, decision: OrchestratorDecision, target: str) -> str:
        from .agent_lifecycle import AgentLifecycleService

        # The model read other agents' transcript text, so its payload is
        # untrusted: only a name crosses. Identity, directory, backend, tools
        # and any fork or resume source come from the agent's own record.
        name = (decision.control_payload or {}).get("name")
        payload: dict[str, Any] = {"name": name.strip()} if isinstance(name, str) and name.strip() else {}
        existing = identity.lookup(target) if target else None
        if existing and (decision.control_action or "").lower() in {
            "relaunch",
            "restart",
            "resume",
        }:
            payload.update(
                name=existing["persona"],
                session=existing["session"],
                replace_sid=existing["session"],
                cwd=existing["cwd"],
                backend=existing["backend"],
            )
        payload["synthesize_audio"] = True
        result = AgentLifecycleService(self.ctx).create(payload)
        return result.session

    def _status_text(self, session: str) -> str:
        agent = identity.lookup(session)
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
        agent = identity.lookup(session)
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
        agent = identity.lookup(session)
        events.broadcast(self.ctx.stream, events.audio(
            url=url, session=session, agent_id=(agent or {}).get("agent_id"),
            voice_id=voice_id, phrase_key=phrase_key, cached_phrase=True))
        return True

    def _replay_last_clip(self, session: str) -> bool:
        agent = identity.lookup(session)
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
        events.broadcast(self.ctx.stream, events.audio(
            url=url, session=session, agent_id=agent["agent_id"],
            voice_id=row["voice_id"], trace_id=row["trace_id"],
            clip_id=row["clip_id"], replay=True))
        return True

    def _valid_target(self, session: str, sessions: set[str]) -> str:
        target = identity.lookup(session) if session in sessions else None
        return session if (target and not target.get("archived_at")
                           and agents_db.interaction_capabilities(target)["can_voice_target"]) else ""

    def _persona_for_session(self, session: str) -> str:
        agent = identity.lookup(session)
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


from .orchestrator_context import (  # noqa: E402,F401  (split_apply: orchestrator)
    build_context_packet, _agent_context_activity, _recent_messages, _latest_state, _compact_state,
    _compact_text, _pending_utterances, _name_candidates,
)
