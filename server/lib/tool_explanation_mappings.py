"""Jev selections as reviewable evidence; rules only by explicit approval.

Agreement is not proof. Repeated confident Jev picks cannot show that an
unfamiliar executable only reads: the same arguments may change something, and
the shipped regression cases say nothing about a program they never saw. So a
selection is only ever evidence. After MIN_EVIDENCE distinct agreeing
activities with no disagreement, for a read/list/search template, and with the
regression cases still passing, a mapping becomes `proposed`, which changes no
explanation. Only `approve()`, an explicit vetting step, makes it a scripted
rule. Every mapping keys on the keyed hash of one exact invocation shape (see
`tool_explanation_templates._identity`), so `--mode=list` and `--mode=delete`
never share a rule, and it is bound to the library version it was proposed
against. Approval and rejection invalidate cached explanations for it.
"""
from __future__ import annotations

import hashlib
import json

from . import db as _db
from .log import log
from . import tool_explanation_templates as templates

MIN_EVIDENCE = 3
LEARN_MIN = 0.90
MAX_EVIDENCE = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_explanation_mappings (
    signature TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    library_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    promoted_at INTEGER
);
CREATE INDEX IF NOT EXISTS tool_explanation_mappings_status ON tool_explanation_mappings(status, library_version);
"""


def activity_hash(activity):
    return hashlib.sha256(json.dumps(activity, sort_keys=True).encode()).hexdigest()[:24]


def promoted():
    """Current-library promoted rules as {signature: template_id}."""
    return {row[0]: row[1] for row in _db.conn().execute(
        "SELECT signature, template_id FROM tool_explanation_mappings WHERE status='promoted' AND library_version=?",
        (templates.VERSION,))}


def regression_failures(signature, template_id):
    """Indexes of shipped regression cases whose result changes with the rule."""
    failures = []
    for index, case in enumerate(templates.REGRESSION):
        _, route = templates.lookup(case["activity"], 1, {signature: template_id})
        if "abstain" in case:
            ok = "template_id" not in route and route.get("reason") == case["abstain"]
        else:
            ok = route.get("template_id") == case["template"] and route.get("parameters", {}) == case["parameters"]
        if not ok:
            failures.append(index)
    return failures


def _invalidate(db, signature):
    db.execute("DELETE FROM tool_explanation_cache WHERE signature=?", (signature,))


def record(route, template_id, confidence, activity):
    """Record one validated Jev selection; return the mapping's status.

    Returns `rejected:<why>` without storing anything for a selection that
    could never become a rule, so one bad answer cannot seed a candidate.
    """
    signature = route.get("signature") or ""
    template = templates.TEMPLATES.get(template_id)
    if route.get("reason") not in templates.JEV_REASONS or not signature or signature in {"shell", "tool:"}:
        return "rejected:ineligible_route"
    if template is None:
        return "rejected:unknown_template"
    if template["action"] not in templates.LEARNABLE_ACTIONS:
        return "rejected:unsafe_action"
    if confidence < LEARN_MIN:
        return "rejected:low_confidence"
    evidence_id = activity_hash(activity)
    now = _db.now_ms()
    db = _db.conn()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT template_id, library_version, status, evidence_json, conflicts FROM tool_explanation_mappings WHERE signature=?",
                         (signature,)).fetchone()
        if row is None or row[1] != templates.VERSION:
            db.execute("INSERT INTO tool_explanation_mappings(signature,template_id,library_version,status,evidence_json,created_at,updated_at) "
                       "VALUES(?,?,?,'candidate','[]',?,?) ON CONFLICT(signature) DO UPDATE SET template_id=excluded.template_id,"
                       "library_version=excluded.library_version,status='candidate',evidence_json='[]',conflicts=0,reason='',updated_at=excluded.updated_at,promoted_at=NULL",
                       (signature, template_id, templates.VERSION, now, now))
            row = (template_id, templates.VERSION, "candidate", "[]", 0)
        status = row[2]
        if status in {"promoted", "rejected", "proposed"}:
            db.execute("COMMIT")
            return status
        if row[0] != template_id:
            db.execute("UPDATE tool_explanation_mappings SET status='conflicted',conflicts=conflicts+1,reason=?,updated_at=? WHERE signature=?",
                       (f"disagreement:{template_id}", now, signature))
            db.execute("COMMIT")
            return "conflicted"
        if status == "conflicted":
            db.execute("COMMIT")
            return status
        evidence = json.loads(row[3])
        if evidence_id not in {item["id"] for item in evidence}:
            evidence = [*evidence, {"id": evidence_id, "confidence": round(confidence, 4), "at": now}][-MAX_EVIDENCE:]
        status, reason = "candidate", ""
        if len(evidence) >= MIN_EVIDENCE:
            failures = regression_failures(signature, template_id)
            if failures:
                status, reason = "rejected", "regression:" + ",".join(map(str, failures))
            else:
                status = "proposed"
        db.execute("UPDATE tool_explanation_mappings SET status=?,evidence_json=?,reason=?,updated_at=? WHERE signature=?",
                   (status, json.dumps(evidence), reason, now, signature))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if status != "candidate":
        log("toolExplanationMapping", f"signature={signature!r} template={template_id} status={status} reason={reason}")
    return status


def approve(signature, template_id, *, reviewer):
    """Vet one proposed mapping into a scripted rule for its exact shape.

    Refuses anything not proposed for this template under the current library,
    and re-runs the regression cases, so approval cannot outrun the evidence.
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("approval needs a named reviewer")
    template = templates.TEMPLATES.get(template_id)
    if template is None or template["action"] not in templates.LEARNABLE_ACTIONS:
        raise ValueError("only read, list or search templates can be approved")
    db = _db.conn()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT template_id, library_version, status FROM tool_explanation_mappings WHERE signature=?",
                         (signature,)).fetchone()
        if row is None or tuple(row) != (template_id, templates.VERSION, "proposed"):
            raise ValueError("mapping is not proposed for this template and library version")
        if regression_failures(signature, template_id):
            raise ValueError("regression cases fail with this mapping")
        now = _db.now_ms()
        db.execute("UPDATE tool_explanation_mappings SET status='promoted',reason=?,updated_at=?,promoted_at=? WHERE signature=?",
                   (f"approved:{reviewer.strip()[:80]}", now, now, signature))
        _invalidate(db, signature)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    log("toolExplanationMapping", f"signature={signature!r} template={template_id} status=promoted reviewer={reviewer.strip()[:80]!r}")


def reject(signature, reason="manual"):
    """Withdraw a rule or candidate and drop explanations it produced."""
    db = _db.conn()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE tool_explanation_mappings SET status='rejected',reason=?,updated_at=?,promoted_at=NULL WHERE signature=?",
                   (reason, _db.now_ms(), signature))
        _invalidate(db, signature)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def listing():
    return [dict(row) for row in _db.conn().execute("SELECT * FROM tool_explanation_mappings ORDER BY updated_at DESC LIMIT 200")]
