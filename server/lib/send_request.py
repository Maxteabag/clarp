"""Parsing and policy for the body of POST /send.

The handler in `server.py` reads JSON, hands it to `SendRequest.from_payload`,
creates the prompt admission, then either asks the orchestrator or dispatches
directly. Everything about *interpreting* the body — defaults, trimming,
the sender lookup, the origin and synthesize_audio policy — lives here so it
can be tested without a socket.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import agents as agents_db
from . import db, origins
from .prompt_admissions import PromptAdmission


class SendRequestError(ValueError):
    """A /send body the handler must refuse.

    `status` is the HTTP status; `json_body` says whether the reply is the
    usual `{"error": message}` document or the historical bare text body
    (the empty-text refusal predates the JSON error convention).
    `trace_id` is the id already assigned to the request, when parsing got
    that far, so the access log can still link the refusal to a turn.
    """

    def __init__(self, status: int, message: str, *, json_body: bool = True,
                 trace_id: str = ""):
        super().__init__(message)
        self.status = status
        self.json_body = json_body
        self.trace_id = trace_id


def _resolve_sender_agent_id(sender_raw: str) -> str:
    if not sender_raw:
        return ""
    sender = (agents_db.get_by_session(sender_raw)
              or agents_db.get_by_agent_id(sender_raw))
    return sender["agent_id"] if sender else ""


def _unheard_audio_sessions(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(
        str(value).strip()
        for value in raw[:64]
        if isinstance(value, str) and str(value).strip()
    ))


@dataclass(frozen=True)
class SendRequest:
    text: str
    session: str
    trace_id: str
    # Stable client-authored message id (idempotency key). The client keys
    # its optimistic bubble by it; the durable user row is stored under it so
    # the two match by identity. A client that sends none gets the trace id.
    client_msg_id: str
    transcription_id: str
    # Voice timeline identity for this turn: an explicit utterance id, else
    # the transcription job id the client already sends.
    voice_utterance_id: str
    hands_free: bool
    synthesize_audio: bool
    unheard_audio_sessions: tuple[str, ...]
    orchestrator_fallback: bool
    queue_if_busy: bool
    # Only always-on hands-free dictation gets routed (orchestrator / spoken
    # name). Tap-to-record and typed text (hands_free=false) always go
    # straight to the agent the client has open — no routing, no exceptions.
    force_session: bool
    # Sender identity: when another agent prompts this one, `sender` names
    # it (session or agent_id). The message is then stamped origin=agent so
    # the client renders it with the sender's avatar instead of as the user.
    sender_agent_id: str
    origin: str
    authenticated: bool

    @property
    def channel(self) -> str:
        return "voice" if self.transcription_id else "chat"

    @classmethod
    def from_payload(cls, data: dict, *, default_session: str,
                     trace_id_factory: Callable[[], str],
                     authenticated: bool,
                     orchestrator_fallback: bool = False,
                     resolve_sender: Callable[[str], str] = _resolve_sender_agent_id,
                     ) -> "SendRequest":
        """Interpret a decoded /send body.

        Raises SendRequestError(400) for a malformed transcription id. An
        empty text is *not* rejected here: the handler records the prompt
        admission first and then calls `require_text()`, so a refused send
        still leaves its admission row.
        """
        text = (data.get("text") or "").strip()
        session = (data.get("session") or default_session).strip() or default_session
        trace_id = (data.get("trace_id") or "").strip() or trace_id_factory()
        client_msg_id = (data.get("client_msg_id") or "").strip() or trace_id
        transcription_id = (data.get("transcription_id") or "").strip()
        voice_utterance_id = ((data.get("utterance_id") or "").strip()
                              or transcription_id)[:128]
        if transcription_id:
            from . import transcription_results
            try:
                transcription_id = transcription_results.normalize_job_id(
                    transcription_id)
            except ValueError as e:
                raise SendRequestError(400, str(e), trace_id=trace_id) from e
        synthesize_audio_raw = data.get("synthesize_audio", None)
        hands_free = data.get("hands_free", False) is True
        unheard_audio_sessions = _unheard_audio_sessions(
            data.get("unheard_audio_sessions") or [])
        orchestrator_fallback = (
            bool(orchestrator_fallback)
            or data.get("orchestrator_fallback", False) is True
        )
        queue_if_busy = data.get("queue_if_busy", False) is True
        force_session = (data.get("force_session", False) is True) or (not hands_free)
        sender_agent_id = resolve_sender((data.get("sender") or "").strip())
        origin = (data.get("origin") or "").strip().lower()
        if origin not in origins.CLIENT_SETTABLE_ORIGINS:
            origin = "agent" if sender_agent_id else "user"
        if synthesize_audio_raw is None:
            synthesize_audio = origin == "user"
        else:
            synthesize_audio = (
                synthesize_audio_raw is not False
                if origin == "user"
                else synthesize_audio_raw is True
            )
        return cls(
            text=text, session=session, trace_id=trace_id,
            client_msg_id=client_msg_id, transcription_id=transcription_id,
            voice_utterance_id=voice_utterance_id, hands_free=hands_free,
            synthesize_audio=synthesize_audio,
            unheard_audio_sessions=unheard_audio_sessions,
            orchestrator_fallback=orchestrator_fallback,
            queue_if_busy=queue_if_busy, force_session=force_session,
            sender_agent_id=sender_agent_id, origin=origin,
            authenticated=bool(authenticated),
        )

    def admit(self) -> PromptAdmission:
        """Record the prompt admission for this request (before any refusal
        that depends on the text, so rejected sends are still accounted)."""
        from . import prompt_admissions
        return prompt_admissions.create(
            authenticated_at_admission=self.authenticated,
            origin=self.origin,
            sender_agent_id=self.sender_agent_id,
            channel=self.channel,
            observed_at=db.now_ms(),
            client_admission_id=self.client_msg_id,
            trace_id=self.trace_id,
            original_text=self.text,
        )

    def require_text(self) -> None:
        if not self.text:
            raise SendRequestError(400, "empty text", json_body=False,
                                   trace_id=self.trace_id)
