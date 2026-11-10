"""Moved out of server/lib/orchestrator.py by tools/refactor/split_apply.py (split 'orchestrator', recipe tools/refactor/splits/s9-orchestrator.toml).
orchestrator.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any
from . import backends, config
from .orchestrator_types import DECISION_AGENT_CONTROL, DECISION_AGENT_MESSAGE, DECISION_AMBIGUOUS, DECISION_CLARIFY, DECISION_CONTROL, DECISION_CORRECTION, DECISION_ERROR, DECISION_IGNORED, DECISION_STATUS, DEFAULT_EFFORT, OPENAI_EFFORTS, OPENAI_PROVIDER, OrchestratorDecision, OrchestratorSettings, TYPESAFE_DECISION_MIN, TYPESAFE_PROVIDER, TYPESAFE_TARGET_MIN


_OPENAI_ALIASES = {"openai", "gpt"}


_JSON_RE = re.compile(r"\{.*\}", re.S)


def normalize_provider(raw: str | None) -> str:
    """Canonical routing provider id: a registry backend id or "openai".

    Aliases the registry knows (antigravity, claude-code, open-code) and the
    OpenAI spellings collapse; anything else is returned lower-cased so the
    caller can reject it.
    """
    value = (raw or "").strip().lower()
    if value in _OPENAI_ALIASES:
        return OPENAI_PROVIDER
    if value in {TYPESAFE_PROVIDER, "jev"}:
        return TYPESAFE_PROVIDER
    if value == "claude-code":
        return backends.CLAUDE
    adapter = backends.get(value)
    return adapter.id if adapter else value


def call_model(packet: dict[str, Any], settings: OrchestratorSettings) -> dict[str, Any]:
    """Ask the configured provider for one routing decision.

    OpenAI goes straight to the API. Every other provider is a registry
    backend whose ``routing_cmd``/``routing_text`` build the one-shot argv and
    extract the reply text, so a new CLI can route the moment it says so.
    """
    provider = normalize_provider(settings.provider)
    prompt = _model_prompt(packet)
    if provider == TYPESAFE_PROVIDER:
        return _call_typesafe(packet, settings, prompt)
    if provider == OPENAI_PROVIDER:
        return _call_openai(prompt, settings)
    runner = backends.get(provider)
    if runner is None or not runner.supports_routing:
        raise RuntimeError(f"unsupported orchestrator provider: {settings.provider}")
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
        env={**os.environ, **runner.routing_env()},
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


def _typesafe_questions(packet: dict[str, Any]) -> dict[str, dict]:
    from . import judgments
    agents = packet.get("agents") or []
    targets: dict[str, str] = {}
    for agent in agents:
        recent = (agent.get("recent_agent_messages") or [{}])[0].get("text", "")
        targets[str(agent.get("session") or "")] = (
            f"{agent.get('persona') or 'agent'}: {str(recent)[:300]}")
    targets.pop("", None)
    targets["none"] = "It is not a message for any of these agents"
    return {
        "kind": judgments.choice(
            "`user_said` was spoken hands-free to a multi-agent voice app. "
            "What should the app do with it?",
            {
                "agent_message": "Deliver it to one agent as a message, instruction "
                                 "or answer",
                "status_query": "The user asks the app how agents are doing; nothing "
                                "should be delivered to an agent",
                "control": "The user asks to hear the last thing again",
                "agent_control": "The user asks to stop, relaunch, resume or switch "
                                 "to an agent",
                "ignored": "Background speech, talk to a passenger, noise or nonsense "
                           "unrelated to any agent",
            }),
        "target": judgments.choice(
            "Which agent's recent conversation does `user_said` continue or answer? "
            "Judge by content only.", targets),
    }


def _typesafe_state(packet: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the questions refer to; the prompt packet carries more."""
    return {
        "user_said": packet.get("utterance") or "",
        "agents": [
            {
                "session": agent.get("session"),
                "persona": agent.get("persona"),
                "recent_user_messages": agent.get("recent_user_messages") or [],
                "recent_agent_messages": agent.get("recent_agent_messages") or [],
            }
            for agent in (packet.get("agents") or [])
        ],
    }


def _call_typesafe(packet: dict[str, Any], settings: OrchestratorSettings,
                   prompt: str) -> dict[str, Any]:
    """Route with a typed decision model instead of a chat completion.

    Jev picks; it cannot write. A clarify decision has to speak a question back
    to the user, so that one branch is handed to OpenAI when a key exists. The
    returned dict is the same shape `parse_decision` already consumes.
    """
    from . import judgments
    questions = _typesafe_questions(packet)
    answers = judgments.judge(
        "router", _typesafe_state(packet), questions,
        timeout_ms_override=max(250, min(settings.timeout_ms, 5000)),
        trace_id=str(packet.get("trace_id") or ""))
    if answers is None:
        # Switched off, no key, or unreachable: keep the configured fallback.
        if config.load().openai_key():
            return _call_openai(prompt, settings)
        raise RuntimeError("typesafe judgments unavailable and no OpenAI key configured")

    kind = answers.choice("kind")
    target = answers.choice("target")
    target_probability = answers.probability("target", target)
    reason = (f"jev action: {answers.top('kind')} | agent: {answers.top('target')}")

    if kind == "agent_message":
        # No agent's conversation fits: this is background speech, not a message.
        if target == "none" and answers.probability("target", "none") >= 0.5:
            kind, target = "ignored", ""
        elif target == "none" or target_probability < TYPESAFE_TARGET_MIN:
            kind = "ambiguous"
    if answers.confidence("kind") < TYPESAFE_DECISION_MIN:
        kind = "ambiguous"

    if kind in ("clarify", "ambiguous") and config.load().openai_key():
        # Asking the user who they meant requires generated speech.
        decision = _call_openai(prompt, settings)
        decision.setdefault("reason", reason)
        return decision

    judgments.record_outcome("router", f"{kind}:{target}" if target else kind)
    return {
        "kind": kind,
        "target_session": target if kind in ("agent_message", "agent_control") else "",
        "confidence": round(float(answers.confidence("kind")), 4),
        "addressing": kind == "agent_message",
        "text_to_send": packet.get("utterance") or "",
        "reason": reason,
    }


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
