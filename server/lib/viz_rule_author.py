"""Grow the fleet map's vocabulary by authoring rules, never classifications.

The map's hot path is a dictionary lookup (`viz_normalize`). This module is the
cold path: it reads the tools no rule matched, asks a model what they are, and
appends *rules*. The model never sees an individual event and never runs during
a render.

That split is the whole design. A model in the render path is non-deterministic,
so the same command lands on a different node between runs, and a map whose
nodes move is worse than a log. Here the model runs once per newly-discovered
tool, its output is validated against a closed vocabulary, and the result is a
reviewable diff.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
from typing import Any, Callable

from . import viz_archetypes, viz_normalize
from .log import log, log_exception

# One model call per run, not per event. Swap freely: anything that takes a
# prompt and returns text satisfies this.
DEFAULT_MODEL_CMD = ["claude", "-p", "--output-format", "text"]
MODEL_TIMEOUT_S = 120

_EXE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_VALID_VERBS = frozenset(viz_archetypes.VERB_ARCHETYPE)
_VALID_KINDS = frozenset({
    "repo", "file", "path", "script", "toolchain", "database", "container",
    "service", "host", "agent", "asset", "self",
})


def pending_path() -> pathlib.Path:
    from . import xdg
    return xdg.data_dir() / "viz_rule_proposals.json"


# --- the prompt -----------------------------------------------------------
def build_prompt(clusters: list[dict]) -> str:
    """Ask for rules over a closed vocabulary, with the unfixable ones excluded."""
    usable = [c for c in clusters if c["count"] > c.get("clamped", 0)]
    lines = [
        f"- {c['hint']!r} seen {c['count']}x; example: {c['example'][:150]}"
        for c in usable[:30]
    ]
    return (
        "You are extending the vocabulary of a fleet-activity map that shows "
        "what coding agents are doing.\n\n"
        "Each unrecognised command below needs one rule: which executable it "
        "is, what kind of action it performs (the verb), and what kind of "
        "thing it acts on (the target kind).\n\n"
        f"Allowed verbs (choose exactly one):\n  {', '.join(sorted(_VALID_VERBS))}\n\n"
        f"Allowed target kinds (choose exactly one):\n  {', '.join(sorted(_VALID_KINDS))}\n\n"
        "Unrecognised commands:\n" + "\n".join(lines) + "\n\n"
        "Reply with ONLY a JSON array, no prose or code fence. Each element:\n"
        '  {"exe": "<executable basename>", "verb": "<verb>", '
        '"kind": "<target kind>", "why": "<short reason>"}\n\n'
        "Omit anything you cannot identify confidently; a missing rule is far "
        "better than a wrong one, because a wrong rule silently mislabels "
        "every future event from that tool. Do not invent verbs or kinds "
        "outside the lists above."
    )


# --- validation -----------------------------------------------------------
def validate(proposal: Any) -> tuple[bool, str]:
    """Gate a single proposal. The vocabulary is closed on purpose."""
    if not isinstance(proposal, dict):
        return False, "not an object"
    exe = str(proposal.get("exe") or "").strip()
    verb = str(proposal.get("verb") or "").strip()
    kind = str(proposal.get("kind") or "").strip()
    if not _EXE_RE.match(exe):
        return False, f"exe {exe!r} is not a plain executable name"
    if verb not in _VALID_VERBS:
        return False, f"verb {verb!r} is outside the vocabulary"
    if kind not in _VALID_KINDS:
        return False, f"kind {kind!r} is outside the vocabulary"
    if exe in viz_normalize.EXE_RULES:
        return False, f"{exe!r} already has a rule"
    return True, ""


def parse_proposals(text: str) -> tuple[list[dict], list[str]]:
    """-> (accepted, rejections). Never raises on bad model output."""
    raw = (text or "").strip()
    if raw.startswith("```"):                 # tolerate a fenced reply
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return [], ["no JSON array in model output"]
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        return [], [f"model output is not valid JSON: {e}"]
    if not isinstance(items, list):
        return [], ["model output is not a JSON array"]

    accepted, rejected, seen = [], [], set()
    for item in items:
        ok, why = validate(item)
        exe = str(item.get("exe", "?")) if isinstance(item, dict) else "?"
        if not ok:
            rejected.append(f"{exe}: {why}")
            continue
        if exe in seen:
            rejected.append(f"{exe}: duplicate in the same reply")
            continue
        seen.add(exe)
        accepted.append({
            "exe": exe, "verb": item["verb"], "kind": item["kind"],
            "why": str(item.get("why", ""))[:200],
            "archetype": viz_archetypes.archetype_for(item["verb"]),
        })
    return accepted, rejected


# --- the run --------------------------------------------------------------
def _call_model(prompt: str) -> str:
    out = subprocess.run(DEFAULT_MODEL_CMD, input=prompt, text=True,
                         capture_output=True, timeout=MODEL_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError(f"model exited {out.returncode}: {out.stderr[:300]}")
    return out.stdout


def propose(clusters: list[dict],
            model: Callable[[str], str] | None = None) -> dict[str, Any]:
    """One model call over the unmatched clusters; returns validated rules.

    Nothing is applied here. Proposals are persisted for review, because a
    rule mislabels every future event from its tool -- the cost of a wrong
    one is not one bad row, it is a permanently wrong node.
    """
    if not clusters:
        return {"proposals": [], "rejected": [], "asked": 0}
    call = model or _call_model
    prompt = build_prompt(clusters)
    try:
        reply = call(prompt)
    except Exception as e:                     # noqa: BLE001
        log_exception("vizRuleAuthorModelFail", e)
        return {"proposals": [], "rejected": [f"model call failed: {e}"],
                "asked": len(clusters)}
    accepted, rejected = parse_proposals(reply)
    log("vizRuleAuthor",
        f"asked={len(clusters)} accepted={len(accepted)} rejected={len(rejected)}")
    return {"proposals": accepted, "rejected": rejected, "asked": len(clusters)}


def save_proposals(result: dict[str, Any]) -> pathlib.Path:
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_proposals()
    by_exe = {p["exe"]: p for p in existing}
    by_exe.update({p["exe"]: p for p in result.get("proposals", [])})
    path.write_text(json.dumps(sorted(by_exe.values(), key=lambda p: p["exe"]),
                               indent=2))
    return path


def load_proposals() -> list[dict]:
    path = pending_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def as_source_lines(proposals: list[dict]) -> str:
    """Proposals as EXE_RULES source, so promoting them is a reviewable diff."""
    return "\n".join(
        f'    "{p["exe"]}": ("{p["verb"]}", "{p["kind"]}"),'
        f'   # {p.get("why", "")[:60]}'
        for p in sorted(proposals, key=lambda p: p["exe"])
    )
