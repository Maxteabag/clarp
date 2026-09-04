"""Wire-level protocol constants shared by server and hooks."""
from __future__ import annotations


class SSEType:
    AUDIO = "audio"
    SERVER_VERSION = "server-version"
    REMOTE_ACTION = "remote-action"
    AGENT_STATE = "agent-state"
    AGENT_ACTIVITY = "agent-activity"
    AGENT_ROSTER = "agent-roster"
    AGENT_FOCUS = "agent-focus"
    TRANSCRIPT_UPDATED = "transcript-updated"
    QUEUE_UPDATED = "queue-updated"
    ARTIFACT_UPDATED = "artifact-updated"
    ATTENTION_UPDATED = "attention-updated"
    BACKGROUND_JOB_UPDATED = "background-job-updated"
    PROVIDER_LIMIT = "provider-limit"
    # Durable server-side decision that this completed turn should push, badge,
    # and mark unread for the user. Clients must not infer this from raw transcript
    # or state transitions.
    # Clean-break generic wire name. Server and clients carrying this protocol
    # must be rolled out as one release family; personalized aliases are not
    # retained or emitted.
    USER_NOTIFICATION = "user-notification"
    # Voice synthesis failed (e.g. ElevenLabs quota exceeded / not configured).
    # Surfaced to the client so a TTS failure isn't just silent audio.
    TTS_ERROR = "tts-error"
    # An agent asked the app for the user's current location; the app surfaces a
    # one-tap Share-location prompt for that session, then POSTs the fix.
    LOCATION_REQUEST = "location-request"
    # An agent asked the app to create an Apple Calendar event. The iOS app owns
    # OS permission and writes the event after the user has granted calendar access.
    CALENDAR_REQUEST = "calendar-request"


class AgentBackend:
    """Bundled backend ids this server build knows how to run.

    The live allow-list is ``lib.backends.ids()`` / ``/agent-model-options``.
    Clients treat ``backend`` as an open string so a newer Host can advertise
    a provider this app binary has never seen.
    """
    CLAUDE = "claude"
    CODEX = "codex"
    AGY = "agy"
    GROK = "grok"
    OPENCODE = "opencode"

    @classmethod
    def valid(cls) -> set[str]:
        return {cls.CLAUDE, cls.CODEX, cls.AGY, cls.GROK, cls.OPENCODE}


class ClipStatus:
    SYNTHESIZED = "synthesized"
    BROADCAST = "broadcast"
    QUEUED = "queued"
    HELD = "held"
    PLAY_START = "play-start"
    PLAY_OK = "play-ok"
    PLAY_FAIL = "play-fail"

    @classmethod
    def valid(cls) -> set[str]:
        return {
            cls.SYNTHESIZED,
            cls.BROADCAST,
            cls.QUEUED,
            cls.HELD,
            cls.PLAY_START,
            cls.PLAY_OK,
            cls.PLAY_FAIL,
        }


class ClipProducerStatus:
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"

    @classmethod
    def valid(cls) -> set[str]:
        return {cls.STREAMING, cls.COMPLETE, cls.FAILED}


class AgentState:
    THINKING = "thinking"
    TOOL = "tool"
    IDLE = "idle"
    SPAWNED = "spawned"
    STOPPED = "stopped"
    # New: explicit turn-complete kind. THINKING/TOOL → DONE on Stop hook.
    # Drives the dock badge (badge lights up only on DONE, not on every
    # interim clip/state event).
    DONE = "done"
    # Claude is compacting its context window — fires from PreCompact hook,
    # clears when the next assistant message lands or the turn finishes.
    COMPACTING = "compacting"
    # Claude is waiting for user input (permission prompt, etc.) — fires
    # from Notification hook. Detail carries the message string.
    WAITING = "waiting"
    # A turn was cut short and not recovered: connection dropped and the
    # auto-retries were exhausted, the API was overloaded / rate limited,
    # or the turn was deliberately aborted. Surfaced to the user as a badge
    # (like WAITING) so a dropped turn doesn't silently look idle. Detail
    # carries {"reason": <error_classify category>, "error": <text>}.
    INTERRUPTED = "interrupted"
    # An out-of-band background task is running (e.g. an agent watching a CI
    # build). NOT busy — the agent isn't producing a turn — but distinct from
    # idle/done so the UI shows a neutral "background task" indicator instead of
    # a misleading "Working" spinner. Detail may carry {"label": <text>}.
    BACKGROUND = "background"

    @classmethod
    def busy_states(cls) -> set[str]:
        # COMPACTING is treated as busy: the agent is still working, just
        # not in a tool call. WAITING / INTERRUPTED / BACKGROUND are NOT busy —
        # the agent is paused for the user, or doing out-of-band work.
        return {cls.THINKING, cls.TOOL, cls.COMPACTING}

    @classmethod
    def valid(cls) -> set[str]:
        return {
            cls.THINKING, cls.TOOL, cls.IDLE, cls.SPAWNED, cls.STOPPED,
            cls.DONE, cls.COMPACTING, cls.WAITING, cls.INTERRUPTED, cls.BACKGROUND,
        }

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.valid()


class ActivityStatus:
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    RECORDED = "recorded"

    @classmethod
    def valid(cls) -> set[str]:
        return {cls.RUNNING, cls.OK, cls.ERROR, cls.RECORDED}


class ClientAction:
    RECORD = "record"
    RECORD_TOGGLE = "record-toggle"
    STOP_AGENT = "stop-agent"
    CONTROLLER_EVENT = "controller-event"

    @classmethod
    def valid(cls) -> set[str]:
        return {
            cls.RECORD,
            cls.RECORD_TOGGLE,
            cls.STOP_AGENT,
            cls.CONTROLLER_EVENT,
        }


class TurnSource:
    PWA = "pwa"
    LOCAL = "local"
    PWA_VOICE_MARKER = "pwa-voice"

    @classmethod
    def valid(cls) -> set[str]:
        return {cls.PWA, cls.LOCAL}
