"""Leader-only decision memory, standing orders, and user values injection."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import uuid
from typing import Any

from . import db
from . import xdg


def _root_candidates() -> list[pathlib.Path]:
    here = pathlib.Path(__file__).resolve()
    candidates = [
        pathlib.Path.cwd(),
        here.parents[1],  # installed root: ~/.local/share/clarp/lib/...
        here.parents[2],  # source root: repo/server/lib/...
    ]
    seen: set[pathlib.Path] = set()
    out: list[pathlib.Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


REPO_ROOT = _root_candidates()[0]
USER_VALUES_DOC = xdg.config_dir() / "user-values.md"

LEADER_STANDING_ORDERS_V2 = """# LEADER STANDING ORDERS v2

Role: decide, delegate, track, and learn. Do not implement work yourself unless the user explicitly changes your role for this turn.

Principle: discover what is valuable; fence RISK and TIME, never the activity.

Keep these layers separate:
- Persona: how you sound.
- User values: what the user tends to find important and valuable.
- Authority: these orders, explicit user instruction, and risk/time guardrails.

Leader operating loop:
1. Turn the user's intent into objective, proof, owner, risk class, and time budget.
2. Search decision memory and user values before asking the user a judgment question.
3. If covered, reuse it, log the application, and continue.
4. If novel, ask once with a recommended default and consequence; log the answer and merge durable lessons into user values.
5. Delegate execution to workers with self-prompt --from your own session.
6. Track work in the task/run ledger and team feed; unstick stalled workers and dedupe overlap.
7. Report only meaningful transitions: delegated, blocked, needs decision, verified complete, or failed with evidence.

What the leader may do autonomously:
- Read context: files, docs, logs, traces, DB rows, team feed, prior decisions, and user values.
- Decide task boundaries, owners, priority, risk class, and time budgets.
- Prompt workers with bounded tasks and expected proof.
- Inspect worker reports/evidence; reroute or re-prompt stalled workers.
- Run decision-memory helpers and lightweight checks that find valuable next moves.

What the leader must delegate, not do directly:
- Code edits, refactors, tests, builds, commits, PRs, deploys, scraping, runtime debugging, product implementation, long research, and experiments.

Ask the user first:
- External messages/emails, spending, bookings, buying, real-world obligations.
- Durable data deletion, hard-to-reverse account changes, credential/security/privacy/legal/financial/medical/employment/relationship-impacting actions.
- Merge, release, publish, or deploy authority when not explicitly granted.
- Continuing past the time budget without useful evidence.

Risk/time guardrails:
- Under 15 min: delegate low-risk useful work without asking.
- 15-60 min: delegate with visible run record and stop condition.
- Over 60 min: split, checkpoint, or ask priority.
- Ambiguous risk: reduce risk first by inspecting, simulating, dry-running, or asking narrower.

User defaults:
- Evidence first; claims need runtime proof, especially iOS/native when relevant.
- No silent skips; meaningful failures/skips must be durable and visible.
- Explicit ownership: owner, location, state, blocker, proof.
- Fewer permission prompts: ask only for taste, priority, consent, authority, risk, or irreversible choices.
- Put information where the user naturally looks.
- Visible self-statuses must be header-sized: 2-3 words, under 20 chars (good: "Awaiting Domi", "Reviewing PR", "Building"; bad: a full sentence).

Decision capture: before asking the user a judgment call, search memory. Reuse covered answers and log application. If novel, ask once, log the answer, and merge durable lessons into user values. Re-ask only when context materially changed.
"""


def normalize_question(question: str) -> str:
    return " ".join(question.strip().lower().split())


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def search_decisions(question: str, *, tags: list[str] | None = None,
                     limit: int = 5) -> list[dict[str, Any]]:
    """Return active prior judgments that could answer `question`.

    This deliberately starts with deterministic matching. Semantic retrieval can
    layer on later, but the first version must be inspectable and testable.
    """
    q_norm = normalize_question(question)
    q_hash = question_hash(question)
    tag_terms = [t.lower() for t in (tags or []) if t.strip()]
    rows = db.conn().execute(
        """SELECT * FROM decisions
            WHERE status = 'active'
              AND (question_hash = ?
                   OR lower(canonical_question) LIKE ?
                   OR lower(tags) LIKE ?)
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?""",
        (
            q_hash,
            f"%{q_norm[:80]}%" if q_norm else "%",
            f"%{tag_terms[0]}%" if tag_terms else "\u0000",
            int(limit),
        ),
    ).fetchall()
    return [_decision_dict(row) for row in rows]


def search_user_value_facts(question: str, *, tags: list[str] | None = None,
                      limit: int = 5) -> list[dict[str, Any]]:
    q_norm = normalize_question(question)
    tag_terms = [t.lower() for t in (tags or []) if t.strip()]
    rows = db.conn().execute(
        """SELECT * FROM user_value_facts
            WHERE status IN ('candidate', 'promoted')
              AND (lower(statement) LIKE ?
                   OR lower(tags) LIKE ?)
            ORDER BY status DESC, evidence_count DESC, updated_at DESC
            LIMIT ?""",
        (
            f"%{q_norm[:80]}%" if q_norm else "%",
            f"%{tag_terms[0]}%" if tag_terms else "\u0000",
            int(limit),
        ),
    ).fetchall()
    return [_user_value_fact_dict(row) for row in rows]


def search_memory(question: str, *, tags: list[str] | None = None,
                  limit: int = 5) -> dict[str, Any]:
    return {
        "decisions": search_decisions(question, tags=tags, limit=limit),
        "user_value_facts": search_user_value_facts(question, tags=tags, limit=limit),
    }


def get_decision(decision_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        "SELECT * FROM decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    return _decision_dict(row) if row else None


def log_decision(*, question: str, user_answer: str,
                 normalized_answer: str = "", decision_type: str = "workflow",
                 context: dict[str, Any] | None = None,
                 scope: dict[str, Any] | None = None,
                 tags: list[str] | None = None,
                 risk_class: str = "low",
                 time_horizon: str = "until_changed",
                 confidence: float = 1.0,
                 source_trace: str = "",
                 source_message_id: str = "",
                 source_agent_id: str = "") -> str:
    now = db.now_ms()
    decision_id = f"dec_{uuid.uuid4().hex}"
    canonical = " ".join(question.strip().split())
    db.conn().execute(
        """INSERT INTO decisions (
               id, canonical_question, question_hash, context, user_answer,
               normalized_answer, decision_type, scope, tags, risk_class,
               time_horizon, confidence, status, source_trace,
               source_message_id, source_agent_id, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
        (
            decision_id,
            canonical,
            question_hash(canonical),
            _json(context or {}),
            user_answer.strip(),
            (normalized_answer or user_answer).strip(),
            decision_type,
            _json(scope or {}),
            _json(tags or []),
            risk_class,
            time_horizon,
            float(confidence),
            source_trace,
            source_message_id,
            source_agent_id,
            now,
            now,
        ),
    )
    return decision_id


def log_application(decision_id: str, *, task_id: str = "", run_id: str = "",
                    trace_id: str = "", applied_context: dict[str, Any] | None = None,
                    outcome: str = "used", reason: str = "") -> str:
    application_id = f"dapp_{uuid.uuid4().hex}"
    db.conn().execute(
        """INSERT INTO decision_applications (
               application_id, decision_id, task_id, run_id, trace_id,
               applied_context, outcome, reason, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            application_id,
            decision_id,
            task_id,
            run_id,
            trace_id,
            _json(applied_context or {}),
            outcome,
            reason,
            db.now_ms(),
        ),
    )
    return application_id


def upsert_user_value_fact(*, statement: str, category: str = "preference",
                     decision_id: str = "", scope: dict[str, Any] | None = None,
                     tags: list[str] | None = None,
                     confidence: float = 0.7) -> str:
    clean = " ".join(statement.strip().split())
    if not clean:
        raise ValueError("statement is required")
    c = db.conn()
    now = db.now_ms()
    existing = c.execute(
        """SELECT fact_id, evidence_count, confidence FROM user_value_facts
            WHERE lower(statement) = lower(?)
              AND category = ?
              AND status IN ('candidate', 'promoted')
            ORDER BY updated_at DESC
            LIMIT 1""",
        (clean, category),
    ).fetchone()
    if existing:
        fact_id = existing["fact_id"]
        c.execute(
            """UPDATE user_value_facts
                  SET evidence_count = evidence_count + 1,
                      confidence = max(confidence, ?),
                      updated_at = ?
                WHERE fact_id = ?""",
            (float(confidence), now, fact_id),
        )
        return fact_id
    fact_id = f"sf_{uuid.uuid4().hex}"
    c.execute(
        """INSERT INTO user_value_facts (
               fact_id, decision_id, statement, category, scope, tags,
               evidence_count, confidence, status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'candidate', ?, ?)""",
        (
            fact_id,
            decision_id or None,
            clean,
            category,
            _json(scope or {}),
            _json(tags or []),
            float(confidence),
            now,
            now,
        ),
    )
    return fact_id


def promote_user_value_fact(fact_id: str) -> None:
    db.conn().execute(
        "UPDATE user_value_facts SET status = 'promoted', updated_at = ? WHERE fact_id = ?",
        (db.now_ms(), fact_id),
    )


def latest_promotion_activity() -> int:
    row = db.conn().execute(
        "SELECT MAX(updated_at) AS ts FROM user_value_facts WHERE status = 'promoted'"
    ).fetchone()
    return int((row and row["ts"]) or 0)


CORE_USER_VALUE_SECTION_HEADINGS = (
    "## Current Value Model",
    "## Anti-Goals",
    "## Risk Posture",
)


def compact_user_values(max_chars: int = 1800) -> str:
    curated = _extract_core_user_value_sections(_strip_recent_promotions(_read_user_values_doc()))
    text = curated or "No user values document has been seeded yet."
    if not text:
        return "No user values document has been seeded yet."
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[Compact user values truncated for turn context.]"


def _user_values_doc_candidates() -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    env_path = os.environ.get("CLARP_USER_VALUES_DOC", "").strip()
    if env_path:
        candidates.append(pathlib.Path(env_path))
    candidates.append(USER_VALUES_DOC)
    for root in _root_candidates():
        candidates.append(root / "docs" / "user-values.example.md")
    seen: set[pathlib.Path] = set()
    out: list[pathlib.Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _read_user_values_doc() -> str:
    for candidate in _user_values_doc_candidates():
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


def _strip_recent_promotions(text: str) -> str:
    lines = str(text or "").splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == "## Recent Promotions":
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


def _extract_core_user_value_sections(text: str) -> str:
    """Keep the per-turn user values bounded to durable operating doctrine.

    Promotions stay queryable in SQLite via search_memory/search_user_value_facts;
    they are intentionally not dumped into every leader turn.
    """
    clean = str(text or "").strip()
    if not clean:
        return ""
    lines = clean.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped if stripped in CORE_USER_VALUE_SECTION_HEADINGS else None
        if current:
            sections.setdefault(current, []).append(line)
    if not sections:
        return clean
    out = ["# User Values", "", "Core per-turn value model. Search SQLite memory for promoted facts."]
    for heading in CORE_USER_VALUE_SECTION_HEADINGS:
        section = sections.get(heading)
        if not section:
            continue
        out.extend(["", *section])
    return "\n".join(out).strip()


def _with_recent_promotions(curated: str) -> str:
    recent = _render_recent_promotions()
    if not recent:
        return curated.strip()
    return curated.rstrip() + "\n\n" + recent


def _render_recent_promotions() -> str:
    rows = db.conn().execute(
        """SELECT fact_id, category, statement
             FROM user_value_facts
            WHERE status = 'promoted'
            ORDER BY updated_at DESC, created_at DESC"""
    ).fetchall()
    if not rows:
        return ""
    lines = ["## Recent Promotions"]
    for row in rows:
        lines.append(
            f"- <!-- user-value-fact:{row['fact_id']} --> "
            f"[{row['category']}] {row['statement']}"
        )
    return "\n".join(lines)


def leader_context_instruction(*, leader_session: str = "") -> str:
    helper = (
        "## Decision Memory CLI\n"
        "- Search before asking: `python3 scripts/leader_decision.py search --question \"...\" --tags tag1,tag2`\n"
        "- Covered: `python3 scripts/leader_decision.py apply --decision-id dec_... --reason \"reused for this task\"`\n"
        "- Novel: ask once with recommended default/consequence, then `python3 scripts/leader_decision.py log --question \"...\" --answer \"...\" --type workflow --tags tag1,tag2`\n"
        "- Durable user values: `python3 scripts/leader_decision.py user-values --statement \"...\" --category preference --promote` or `--decision-id dec_... --promote`\n"
    )
    session_line = (
        f"\nDelegate with `--from {leader_session}`." if leader_session else ""
    )
    return "\n\n".join([
        LEADER_STANDING_ORDERS_V2,
        helper + session_line,
        "## Compact User Values\n" + compact_user_values(),
    ])


def merge_decision_to_user_values(*, decision_id: str = "", statement: str = "",
                           category: str = "preference",
                           promote: bool = False,
                           scope: dict[str, Any] | None = None,
                           tags: list[str] | None = None) -> str:
    if decision_id and not statement.strip():
        decision = get_decision(decision_id)
        if decision is None:
            raise ValueError(f"decision not found: {decision_id}")
        statement = (
            str(decision.get("normalized_answer") or "").strip()
            or str(decision.get("user_answer") or "").strip()
        )
    if not statement.strip():
        raise ValueError("statement is required unless --decision-id resolves")
    fact_id = upsert_user_value_fact(
        statement=statement,
        category=category,
        decision_id=decision_id,
        scope=scope,
        tags=tags,
        confidence=0.8 if promote else 0.7,
    )
    if promote:
        promote_user_value_fact(fact_id)
    return fact_id


def _decision_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["context"] = _parse_json(out.get("context"), {})
    out["scope"] = _parse_json(out.get("scope"), {})
    out["tags"] = _parse_json(out.get("tags"), [])
    return out


def _user_value_fact_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["scope"] = _parse_json(out.get("scope"), {})
    out["tags"] = _parse_json(out.get("tags"), [])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Leader decision-memory helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search")
    p_search.add_argument("--question", required=True)
    p_search.add_argument("--tags", default="")

    p_log = sub.add_parser("log")
    p_log.add_argument("--question", required=True)
    p_log.add_argument("--answer", required=True)
    p_log.add_argument("--type", default="workflow")
    p_log.add_argument("--tags", default="")
    p_log.add_argument("--risk", default="low")
    p_log.add_argument("--time-horizon", default="until_changed")
    p_log.add_argument("--source-trace", default="")
    p_log.add_argument("--source-message-id", default="")
    p_log.add_argument("--source-agent-id", default="")

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--decision-id", required=True)
    p_apply.add_argument("--reason", default="")
    p_apply.add_argument("--trace-id", default="")

    p_values = sub.add_parser("user-values")
    p_values.add_argument("--statement", default="")
    p_values.add_argument("--category", default="preference")
    p_values.add_argument("--decision-id", default="")
    p_values.add_argument("--tags", default="")
    p_values.add_argument("--promote", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "search":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        print(json.dumps(
            search_memory(args.question, tags=tags),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if args.cmd == "log":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        decision_id = log_decision(
            question=args.question,
            user_answer=args.answer,
            decision_type=args.type,
            tags=tags,
            risk_class=args.risk,
            time_horizon=args.time_horizon,
            source_trace=args.source_trace,
            source_message_id=args.source_message_id,
            source_agent_id=args.source_agent_id,
        )
        print(decision_id)
        return 0
    if args.cmd == "apply":
        print(log_application(
            args.decision_id, reason=args.reason, trace_id=args.trace_id))
        return 0
    if args.cmd == "user-values":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        print(merge_decision_to_user_values(
            decision_id=args.decision_id,
            statement=args.statement,
            category=args.category,
            tags=tags,
            promote=args.promote,
        ))
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
