"""One function per decision that can optionally ask Jev.

`judgments.py` owns the transport; this module owns the policy: the wording of
each question, the threshold, and what the answer means. Thresholds live here
rather than in one shared setting because they are not comparable — discarding
speech, switching an account and picking an agent carry different costs, so they
earn different bars.

Every function falls back to the code Clarp uses today when the model is off,
unavailable, or unsure.
"""
from __future__ import annotations

from typing import Any

from . import judgments

# Discarding real speech is the bug this site exists to fix, so the model has to
# be more than half sure before anything is thrown away. Measured on the
# regression set: at 0.6 every remaining error keeps speech rather than losing it.
JUNK_MIN = 0.60
# A wrong usage-limit verdict can switch accounts on its own, so it is held to a
# higher bar than the other error kinds.
USAGE_LIMIT_MIN = 0.70
ERROR_MIN = 0.50
# Below this no agent is clearly addressed and the caller keeps its own default.
NAME_MIN = 0.60


# ---- junk transcripts (server/lib/hallucinations.py) -------------------

_JUNK_QUESTION = judgments.noul(
    "`transcript` came from a speech recogniser that invents text on silence or "
    "background noise: typically video outros, subtitle credits, calls to subscribe, "
    "music captions and stray fragments. Is `transcript` such an invented artefact "
    "rather than something the user actually said?",
    yes="Invented artefact; the user most likely said nothing",
    no="A plausible thing for a user to say to a coding assistant, even if very short",
)


def is_junk(text: str, *, agent_last_message: str = "") -> bool:
    """True when `text` should be discarded as a transcription artefact.

    Falls back to the phrase blocklist, which cannot tell a real one-word reply
    ("Okay.") from an invented one and discards both.
    """
    from .hallucinations import is_pure_hallucination

    state: dict[str, Any] = {"transcript": text}
    if agent_last_message:
        state["agent_last_message"] = agent_last_message
    answer = judgments.judge("junk", state, {"junk": _JUNK_QUESTION})
    if answer is None:
        return is_pure_hallucination(text)
    probability = answer.noul("junk")
    verdict = probability >= JUNK_MIN
    judgments.record_outcome("junk", "discarded" if verdict else "kept")
    return verdict


# ---- why a turn died (server/lib/error_classify.py) --------------------

_ERROR_KINDS = {
    "usage_limit": "The account ran out of quota, credits, usage, or hit a plan, "
                   "session or weekly limit",
    "connection": "The network link broke: DNS failure, socket closed, connection "
                  "reset, fetch failed, offline",
    "transient": "The provider answered but is overloaded, rate limiting by load, "
                 "or returned a 5xx server error",
    "interrupted": "Someone deliberately stopped or aborted the turn",
    "timeout": "The backend reported that it timed out or hung",
    "runner_exit": "The process exited with an error code and no other recognisable cause",
    "unknown": "None of these",
}

_ERROR_QUESTION = judgments.choice(
    "`error_text` is what a coding-agent command line printed when a turn ended "
    "badly. Why did it end?",
    _ERROR_KINDS,
)


def classify_error_or_unknown(text: str, regex_kind: str) -> str:
    """Return `regex_kind` untouched unless it is UNKNOWN, then ask the model.

    UNKNOWN means the dispatcher does nothing: the agent goes quiet and the
    badge still reads Connected. That is the gap this fills.
    """
    if regex_kind != "unknown":
        return regex_kind
    return classify_error(text)


def classify_error(text: str) -> str:
    """Name the failure behind `text`, or 'unknown' to keep today's behaviour.

    Only called once the ordered regexes in `error_classify` have already
    returned UNKNOWN, so this adds a verdict where there was none rather than
    overriding one. Every new backend release can invent wording the regexes
    have never seen; this is what catches it.
    """
    if not (text or "").strip():
        return "unknown"
    answer = judgments.judge("errors", {"error_text": text}, {"kind": _ERROR_QUESTION},
                             timeout_ms_override=5000)  # background: no user waiting
    if answer is None:
        return "unknown"
    kind = answer.choice("kind")
    floor = USAGE_LIMIT_MIN if kind == "usage_limit" else ERROR_MIN
    if kind not in _ERROR_KINDS or answer.confidence("kind") < floor:
        kind = "unknown"
    judgments.record_outcome("errors", kind)
    return kind


# ---- who was spoken to (server/lib/routing.py) -------------------------

def resolve_spoken_name(text: str, agents: dict) -> tuple[str | None, str] | None:
    """Pick the addressed agent, or None to let the caller use its own matcher.

    The exact-name path in `routing` runs first and stays free; this replaces
    only the fuzzy fallback, which cannot tell "Rachel, check the branch" from
    "did Rachel finish?" because it never looks past the spelling.
    """
    names = {str((info or {}).get("name", "")).strip(): session
             for session, info in (agents or {}).items()}
    names = {name: session for name, session in names.items() if name}
    if not text.strip() or not names:
        return None

    criteria = {name: f"The user is speaking TO {name}, giving them the message"
                for name in names}
    criteria["nobody"] = ("The user is not addressing any agent by name. Any name that "
                          "appears is only being talked about, or the speech is not for "
                          "an agent at all")
    question = judgments.choice(
        "`user_said` is a voice transcript and may misspell names that sound alike. "
        "Which of `agents` is the user speaking to?", criteria)
    answer = judgments.judge(
        "naming", {"user_said": text, "agents": sorted(names)}, {"to": question})
    if answer is None:
        return None
    pick = answer.choice("to")
    if pick == "nobody" or pick not in names or answer.probability("to", pick) < NAME_MIN:
        judgments.record_outcome("naming", "nobody")
        return None, text
    judgments.record_outcome("naming", pick)
    return names[pick], text
