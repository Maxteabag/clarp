"""Inert values and codecs shared by runtime transport and turn execution.

Execution, admission authority and persistence depend on this contract; this
module never imports those owners. Historical modules re-export these objects.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class RuntimeProtocolError(RuntimeError):
    pass


class RuntimeUnavailable(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class StopLease:
    lease_id: str
    trace_id: str
    terminated: int
    dropped: int


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def encode_request(method: str, params: dict[str, Any] | None = None) -> bytes:
    method = str(method or "").strip()
    if not method:
        raise RuntimeProtocolError("runtime method is required")
    return (json.dumps({
        "version": PROTOCOL_VERSION,
        "method": method,
        "params": _json_value(params or {}),
    }, separators=(",", ":")) + "\n").encode()


def decode_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RuntimeProtocolError("runtime request is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProtocolError("invalid runtime request") from exc
    if not isinstance(value, dict):
        raise RuntimeProtocolError("runtime request must be an object")
    if value.get("version") != PROTOCOL_VERSION:
        raise RuntimeProtocolError(
            f"unsupported runtime protocol version: {value.get('version')!r}")
    method = value.get("method")
    params = value.get("params", {})
    if not isinstance(method, str) or not method.strip():
        raise RuntimeProtocolError("runtime method is required")
    if not isinstance(params, dict):
        raise RuntimeProtocolError("runtime params must be an object")
    return {"version": PROTOCOL_VERSION, "method": method, "params": params}



@dataclasses.dataclass(frozen=True)
class PromptAdmission:
    admission_id: str
    admission_version: int
    authenticated_at_admission: bool
    cooperative_principal: str
    principal_id: str
    origin: str
    sender_agent_id: str
    channel: str
    observed_at: int
    client_admission_id: str
    trace_id: str
    original_text: str
    # The app that sent it (``"desktop"``), when it said; "" otherwise.
    client: str = ""

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> PromptAdmission | None:
        try:
            value = json.loads(raw or "")
            if not isinstance(value, dict):
                return None
            return cls(
                admission_id=str(value["admission_id"]),
                admission_version=int(value["admission_version"]),
                authenticated_at_admission=bool(
                    value["authenticated_at_admission"]
                ),
                cooperative_principal=str(value["cooperative_principal"]),
                principal_id=str(value.get("principal_id") or ""),
                origin=str(value["origin"]),
                sender_agent_id=str(value.get("sender_agent_id") or ""),
                channel=str(value["channel"]),
                observed_at=int(value["observed_at"]),
                client_admission_id=str(value["client_admission_id"]),
                trace_id=str(value["trace_id"]),
                original_text=str(value["original_text"]),
                client=str(value.get("client") or ""),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


@dataclass(frozen=True)
class DispatchResult:
    session: str
    backend: str
    queued: bool = False
    queue_depth: int = 0
    queue_revision: int = 0
    # F4 S1: the user send's delivery object (launch_receipts.delivery_for_send),
    # read after the dispatch decided; None = unknown. Never retry authority.
    delivery: dict | None = None
    # F4 E2-S1: the identity of the runtime that computed ``delivery``, in the
    # same RPC result (runtime_bridge); None when computed in-process.
    delivery_runtime: dict | None = None


class DispatchError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class DispatchCommand:
    """One send, built once where it enters (``TurnDispatchService.submit``,
    the HTTP send handler, a scheduler adapter) and serialized once across the
    runtime RPC. Admission — the Janitor-demand authority check, the
    leader-tick gate, the durable write — runs on the side that owns turns,
    exactly once per command."""
    text: str
    requested_session: str = ""
    trace_id: str = ""
    synthesize_audio: bool = True
    forced_session: str = ""
    routed_by_orchestrator: bool = False
    client_msg_id: str = ""
    origin: str = "user"
    sender_agent_id: str = ""
    prompt_admission: PromptAdmission | None = None
    prompt_admission_id: str = ""
    queue_if_busy: bool = False
    skip_admission: bool = False
    durable_queue_id: str = ""
    unheard_audio_sessions: tuple[str, ...] = ()
    allow_paused_queue: bool = False
    janitor_run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.unheard_audio_sessions, tuple):
            object.__setattr__(self, "unheard_audio_sessions",
                               tuple(str(item) for item in self.unheard_audio_sessions))

    def as_kwargs(self) -> dict[str, Any]:
        """The historical ``dispatch(**kwargs)`` shape. ``janitor_run_id`` is
        present only when set, so an older runtime keeps accepting ordinary
        work from a newer HTTP server."""
        values = {field.name: getattr(self, field.name)
                  for field in dataclasses.fields(self)}
        if not self.janitor_run_id:
            values.pop("janitor_run_id")
        return values

    def to_wire(self) -> dict[str, Any]:
        values = self.as_kwargs()
        if self.prompt_admission is not None:
            values["prompt_admission"] = dataclasses.asdict(self.prompt_admission)
        values["unheard_audio_sessions"] = list(self.unheard_audio_sessions)
        return values

    @classmethod
    def from_wire(cls, params: dict[str, Any]) -> "DispatchCommand":
        """Parse an RPC payload; ValueError for anything this side cannot run."""
        names = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(params) - names)
        if unknown:
            raise ValueError(f"unknown dispatch fields: {', '.join(unknown)}")
        values = dict(params)
        admission = values.get("prompt_admission")
        if isinstance(admission, (dict, str)):
            raw = admission if isinstance(admission, str) else json.dumps(admission)
            values["prompt_admission"] = PromptAdmission.from_json(raw)
            if values["prompt_admission"] is None:
                raise ValueError("invalid prompt admission")
        elif admission is not None and not isinstance(admission, PromptAdmission):
            raise ValueError("invalid prompt admission")
        if "text" not in values:
            raise ValueError("dispatch text is required")
        values["unheard_audio_sessions"] = tuple(
            str(item) for item in values.get("unheard_audio_sessions") or ())
        return cls(**values)
