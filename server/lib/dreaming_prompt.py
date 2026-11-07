"""Moved out of server/lib/dreaming.py by tools/refactor/split_apply.py (split 'dreaming', recipe tools/refactor/splits/s9-dreaming.toml).
dreaming.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import re
from typing import Any
from . import db, dream_seeds
from .dreaming_settings import DREAMING_OK, DREAM_HIDDEN_PREFIX, DREAM_PRIOR_CONTEXT_MAX_CHARS, DREAM_PRIOR_ROUND_EXCERPT_CHARS, DreamingSettings, _FALLBACK_LINE_RE, _SLATE_LINE_RE, get_settings
from .dreaming_run_lookup import _settings_for_run, get_run


def _parse_slate(text: str, settings: DreamingSettings | None = None) -> list[str]:
    return [item["title"] for item in _parse_slate_items(text, settings=settings)]


def _parse_slate_items(text: str,
                       settings: DreamingSettings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _SLATE_LINE_RE.finditer(text or ""):
        line = match.group(0)
        title = _clean_slate_title(match.group("title"))
        tag = (match.group("tag") or "").strip("[]").lower()
        anti = _slate_item_is_anti_promoted(tag, line)
        evidence_status = _slate_item_evidence_status(tag, anti)
        if title and title not in seen:
            items.append({
                "title": title,
                "anti_promoted": anti,
                "evidence_status": evidence_status,
                "evidence_summary": (
                    "Seed grounded this direction as already fixed."
                    if anti else ""
                ),
                "origin_note": "",
            })
            seen.add(title)
    if len([item for item in items if not item["anti_promoted"]]) >= settings.min_directions:
        return items
    # Degraded path. The old fallback accepted *any* line of 20+ characters,
    # so a failed parse silently branched on section headers and stray prose
    # while looking exactly like a healthy run. Only structured lines are
    # eligible now, and everything produced here is tagged so the ledger shows
    # that the strict parse missed.
    for match in _FALLBACK_LINE_RE.finditer(text or ""):
        stripped = " ".join(match.group("title").split())
        if len(stripped) < 20 or stripped in seen:
            continue
        lower = stripped.lower()
        anti = any(
            marker in lower
            for marker in ("already-fixed", "already fixed",
                           "anti-promoted", "anti promoted")
        )
        items.append({
            "title": _clean_slate_title(stripped),
            "anti_promoted": anti,
            "evidence_status": "refuted" if anti else "speculative",
            "evidence_summary": (
                "Seed grounded this direction as already fixed."
                if anti else ""
            ),
            "origin_note": "fallback-parse: seed did not emit a D<n> slate",
        })
        seen.add(stripped)
        if len(items) >= settings.direction_count * 2:
            break
    return items


def _clean_slate_title(raw: str) -> str:
    title = " ".join(str(raw or "").strip().strip("`").split())
    title = re.sub(r"`\s+is\s+skipped\.?.*$", "", title, flags=re.I)
    title = re.sub(r"\s+is\s+skipped\.?.*$", "", title, flags=re.I)
    return title.strip(" `")


def _slate_item_is_anti_promoted(tag: str, line: str) -> bool:
    haystack = f"{tag} {line}".lower().replace("_", "-")
    return any(
        marker in haystack
        for marker in (
            "already-fixed",
            "already fixed",
            "anti-promote",
            "anti-promoted",
            "anti promoted",
        )
    )


def _slate_item_evidence_status(tag: str, anti_promoted: bool) -> str:
    if anti_promoted:
        return "refuted"
    normalized = tag.lower().replace("_", "-")
    for status in ("confirmed", "refuted", "speculative"):
        if status in normalized:
            return status
    return "speculative"


def _build_prompt(*, run_id: str, round_id: str, stage: str, round_index: int,
                  thread_id: str | None) -> str:
    run = get_run(run_id) or {}
    settings = _settings_for_run(run_id)
    strategy = str(run.get("seed_strategy") or dream_seeds.CONTROL)
    thread = _thread(thread_id) if thread_id else None
    prior_context = _prior_round_context(
        run_id=run_id,
        current_stage=stage,
        current_thread_id=thread_id,
    )
    header = (
        f"{DREAM_HIDDEN_PREFIX} run_id={run_id} round_id={round_id} "
        f"stage={stage.upper()} hidden=true]]"
    )
    guardrails = (
        "Guardrails: stay isolated. Allowed: read files, run read-only/test/"
        "smoke commands, web search/fetch if available, and draft candidate "
        "changes only in the disposable dream worktree. Forbidden: shared-tree "
        "edits, deploys, service restarts, auto-merges, destructive data "
        "changes, spending, and external messages. Refuse forbidden operations "
        "and record them as `Guardrail refused: <operation> | <reason>`."
    )
    budget = (
        f"Nightly budget contract: exactly {settings.direction_count} active directions; "
        f"planned {settings.planned_rounds} rounds; target "
        f"{settings.target_token_budget} tokens over about "
        f"{settings.target_minutes} minutes. This is round {round_index}."
    )
    marker = (
        f"DREAM_STAGE_OUTPUT run_id={run_id} round_id={round_id} "
        f"stage={stage.upper()}"
    )
    if stage == "roleplay_day":
        persona = str(run.get("seed_material") or "someone with a demanding job")
        return "\n".join([
            header,
            "Deep Dreaming ROLE-PLAY round, stage one of two.",
            budget,
            guardrails,
            f"You are {persona}. That is all you are for this round.",
            "Do NOT think about software, this repository, or any product. You do not know what this machine is for. Ignore any code you can see.",
            "Walk through one realistic working day, hour by hour, from waking to finishing.",
            "For each part of the day record: where information lives, what you must hold in your head, where you wait or repeat yourself, what your hands are doing, and what you work around.",
            "Then list the concrete friction moments — the specific instants where something cost you time, attention, accuracy, or patience. Be specific about the situation, not the wish.",
            "Do not propose solutions. Do not mention software. Just the day and where it hurt.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.ROLEPLAY:
        persona = str(run.get("seed_material") or "the persona from the prior round")
        return "\n".join([
            header,
            "Deep Dreaming SEED round (role-play reveal, stage two of two).",
            budget,
            guardrails,
            prior_context,
            f"The prior round recorded a working day as {persona}, written without any knowledge of this project. Those friction moments are in the ledger context above.",
            "Now read the actual project in this worktree — its README, its architecture, what it does.",
            f"Generate exactly {settings.direction_count} candidate directions that come from the COLLISION between that lived friction and this project.",
            "For each: which friction moment it answers, whether this project could serve it today, what would have to change, and whether the gap is worth closing.",
            "Prefer directions that this project's authors would not have thought of from inside the code. Do not force a fit — if a friction moment is simply outside this product, saying so is a valid direction.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.FOREIGN:
        return "\n".join([
            header,
            "Deep Dreaming SEED round (foreign-collision seeding).",
            budget,
            guardrails,
            "Below is a subject with no relationship to this project, and an arbitrary constraint. They were chosen at random.",
            "",
            str(run.get("seed_material") or ""),
            "",
            "Ground yourself in the CURRENT code/state of this worktree. Do not re-dream work that is already fixed.",
            f"Now force a collision. Generate exactly {settings.direction_count} candidate directions for THIS project that only become visible when you hold it next to that unrelated subject or accept that constraint.",
            "Look for structural analogy, not decoration: how the unrelated system handles growth, failure, repair, coordination, or scarcity, and what that suggests here.",
            "A direction that could have been produced without the foreign material is a failed direction. Reach.",
            "For each active direction include: the analogy or constraint it came from, why it may matter here, current evidence inspected, and what a next experiment would prove.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.LENSES:
        return "\n".join([
            header,
            "Deep Dreaming SEED round (lens seeding).",
            budget,
            guardrails,
            "Answer these questions about this project specifically, with evidence from the current code:",
            "",
            str(run.get("seed_material") or ""),
            "",
            "Ground yourself against CURRENT code/state. Do not re-dream work that is already fixed.",
            f"From your answers, generate exactly {settings.direction_count} candidate directions, plus any anti-promoted already-fixed directions you found.",
            "The question is the lens, not the answer — a direction should be something you found by looking through it, not a restatement of the question.",
            "For each active direction include: why it may matter, current evidence inspected, what could go wrong, and what a next experiment would prove.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed":
        return "\n".join([
            header,
            "Deep Dreaming SEED round.",
            budget,
            guardrails,
            "First ground yourself against CURRENT code/state and the snapshot. Do not re-dream work that is already fixed.",
            f"Generate exactly {settings.direction_count} active candidate directions, plus any anti-promoted already-fixed directions you found. Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            "Directions can be refactors, product ideas, assumptions to challenge, or research threads.",
            "For each active direction include: why it may matter, current evidence inspected, what could go wrong, and what a next experiment would prove.",
            "For each already-fixed direction include the evidence that it is already fixed; it will be anti-promoted and skipped.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "fanout":
        title = (thread or {}).get("title") or "Untitled direction"
        idx = (thread or {}).get("thread_index") or "?"
        return "\n".join([
            header,
            f"Deep Dreaming FAN OUT round for D{idx}: {title}",
            budget,
            guardrails,
            prior_context,
            "Investigate this direction deeply as its own thread. Be research-heavy and skeptical.",
            "Test the hypothesis where possible: inspect code/state, run safe read-only/test/smoke commands, and web search/fetch if relevant.",
            "Weigh upside, implementation shape, product value, hidden coupling, operational risk, and what the user would need to see as proof.",
            "Surface non-obvious angles rather than repeating the seed slate.",
            "Include these exact fields near the top: `Evidence status: confirmed|refuted|speculative`, `Altitude: idea|verified|worktree|pr`, `Evidence summary: ...`, and `Artifact: ...` if you create a disposable worktree or PR proposal.",
            "End with a clear provisional verdict: pursue, park, or reject, with why.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "iterate":
        title = (thread or {}).get("title") or "Selected direction"
        idx = (thread or {}).get("thread_index") or "?"
        return "\n".join([
            header,
            f"Deep Dreaming ITERATION round for selected D{idx}: {title}",
            budget,
            guardrails,
            prior_context,
            "Go one layer deeper than the fan-out result. Challenge the strongest assumption, look for a sharper experiment, and compare it against at least two alternatives.",
            "Identify what would make this idea fail, what would make it obviously worth doing, and the smallest safe next step.",
            "Repeat the metadata fields: `Evidence status: confirmed|refuted|speculative`, `Altitude: idea|verified|worktree|pr`, `Evidence summary: ...`, and `Artifact: ...` if applicable.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "completeness":
        return "\n".join([
            header,
            "Deep Dreaming COMPLETENESS CHECK.",
            budget,
            guardrails,
            prior_context,
            "Review the seed, fan-out, and iteration work in the prior dream ledger context.",
            "Ask: what angle did we miss? What orthogonal idea, refactor, risk, or product framing has not been considered?",
            "Add any late-breaking insight only if it changes the final recommendations.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "synthesize":
        return "\n".join([
            header,
            "Deep Dreaming SYNTHESIS round.",
            budget,
            guardrails,
            prior_context,
            "Produce the one visible final output for the user.",
            "Start exactly with: Dream Digest",
            "Synthesize the prior dream ledger context into a rich digest with ranked ideas, reasoning, experiments to try, risks, and what evidence would change the ranking.",
            "For each idea state its altitude explicitly: just an idea, verified finding, disposable worktree, or PR proposal. Do not force artifacts when the honest altitude is lower.",
            "Be concrete. Do not mention implementation was hidden unless it matters. Do not include raw control markers except the required final done marker.",
            "End with this exact final line:",
            f"DREAM_DIGEST_DONE run_id={run_id} round_id={round_id}",
        ])
    raise ValueError(f"unknown dreaming stage: {stage}")


def _prior_round_context(*, run_id: str, current_stage: str,
                         current_thread_id: str | None) -> str:
    if current_stage == "seed":
        return ""
    rows = db.conn().execute(
        """SELECT r.round_index, r.stage, r.thread_id, r.response,
                  r.output_chars, t.thread_index, t.title, t.evidence_status,
                  t.altitude, t.artifact_ref, t.evidence_summary,
                  t.guardrail_refusals
             FROM dream_rounds r
             LEFT JOIN dream_threads t ON t.thread_id = r.thread_id
            WHERE r.run_id = ? AND r.status = 'completed'
            ORDER BY r.round_index""",
        (run_id,),
    ).fetchall()
    if not rows:
        return ""

    lines = [
        "Prior dream ledger context (bounded summary; build on this, do not restart):"
    ]
    if current_thread_id:
        thread = _thread(current_thread_id) or {}
        if thread:
            lines.append(
                "- Current thread: "
                + _thread_label(thread)
                + _metadata_suffix(thread)
            )

    for row in rows:
        item = dict(row)
        label = f"Round {item['round_index']} {str(item['stage']).upper()}"
        if item.get("thread_id"):
            label += f" / {_thread_label(item)}"
        excerpt = _preview(
            item.get("response") or "",
            DREAM_PRIOR_ROUND_EXCERPT_CHARS,
        )
        if not excerpt:
            continue
        lines.append(f"- {label}{_metadata_suffix(item)}: {excerpt}")

    return _cap_context_lines(lines, DREAM_PRIOR_CONTEXT_MAX_CHARS)


def _thread_label(row: dict[str, Any]) -> str:
    idx = row.get("thread_index") or "?"
    title = _preview(row.get("title") or "Untitled direction", 180)
    return f"D{idx}: {title}"


def _metadata_suffix(row: dict[str, Any]) -> str:
    parts: list[str] = []
    evidence = str(row.get("evidence_status") or "").strip()
    altitude = str(row.get("altitude") or "").strip()
    summary = _preview(row.get("evidence_summary") or "", 220)
    artifact = _preview(row.get("artifact_ref") or "", 160)
    refusals = str(row.get("guardrail_refusals") or "").strip()
    if evidence:
        parts.append(f"evidence={evidence}")
    if altitude:
        parts.append(f"altitude={altitude}")
    if summary:
        parts.append(f"summary={summary}")
    if artifact:
        parts.append(f"artifact={artifact}")
    if refusals and refusals != "[]":
        parts.append("guardrail-refusals=yes")
    return f" ({'; '.join(parts)})" if parts else ""


def _cap_context_lines(lines: list[str], limit: int) -> str:
    out: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if out and used + extra > limit:
            remaining = max(0, limit - used - len("\n- [prior context truncated]"))
            if remaining > 120:
                out.append(line[:remaining].rstrip() + "...")
            out.append("- [prior context truncated]")
            break
        out.append(line)
        used += extra
    return "\n".join(out)


def _thread(thread_id: str | None) -> dict[str, Any] | None:
    if not thread_id:
        return None
    row = db.conn().execute(
        "SELECT * FROM dream_threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return dict(row) if row else None


def _preview(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _random_recipe() -> tuple[str, str]:
    strategy = dream_seeds.choose_strategy()
    return strategy, dream_seeds.choose_dose(strategy)
