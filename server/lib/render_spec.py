"""The render_spec v1 contract. Verdicts depend only on the submitted JSON."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .voice_markup import strip_hidden_blocks

_DIR = Path(__file__).resolve().parents[1] / "contract/catalogs"
if not _DIR.is_dir():  # source checkout; installed releases place contract/ beside lib/
    _DIR = _DIR.parent.parent.parent / "contract/catalogs"
CATALOG_BYTES = (_DIR / "clarp.form.v1.json").read_bytes()
_CATALOG = json.loads(CATALOG_BYTES)
_REASONS = json.loads((_DIR / "clarp.form.v1.refusals.json").read_bytes())
CATALOG_SHA256 = "c1:" + hashlib.sha256(CATALOG_BYTES).hexdigest()
_ID = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_SUBMISSION = re.compile(r"[A-Za-z0-9-]{16,80}\Z")
MAX_INTEGER = 2**53 - 1


class JsonObject(dict):
    """Retain duplicate-key evidence without changing legacy form dict access."""
    def __init__(self, pairs):
        super().__init__(pairs)
        self.duplicate = len(self) != len(pairs)


def unique_object(value, path=""):
    if getattr(value, "duplicate", False):
        raise Refusal("invalid_object", path)


def check_unique(value, path="", depth=0, *, reject_on=False):
    if depth > 64:
        raise Refusal("invalid_json", path)
    if isinstance(value, dict):
        unique_object(value, path)
        if reject_on and "on" in value:
            raise Refusal("unknown_key", _path(path, "on"))
        for key in sorted(value, key=_order):
            check_unique(value[key], _path(path, key), depth+1, reject_on=reject_on)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            check_unique(child, _path(path, index), depth+1, reject_on=reject_on)


class Refusal(ValueError):
    def __init__(self, code, path):
        self.code, self.path = code, path
        super().__init__(code)

    def triple(self):
        return {"code": self.code, "path": self.path, "message": _REASONS[self.code]}


def _path(path, key):
    # Only a bounded structural name reaches the response or the log.
    if not isinstance(key, (int, str)) or not re.fullmatch(r"[a-zA-Z0-9_$-]{1,80}", str(key)):
        return path
    return f"{path}/{key}"


def _order(key):
    try:
        return key.encode("utf-16-be")
    except (UnicodeError, AttributeError):
        raise Refusal("invalid_json", "") from None


def _ordered(value, path, depth=0):
    if depth > 64:
        raise Refusal("invalid_json", path)
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise Refusal("number_form", path)
    elif type(value) is float:
        raise Refusal("number_form", path)
    elif isinstance(value, str):
        try:
            value.encode()
        except UnicodeError:
            raise Refusal("invalid_json", path) from None
    elif isinstance(value, list):
        return [_ordered(v, _path(path, i), depth + 1) for i, v in enumerate(value)]
    elif isinstance(value, dict):
        unique_object(value, path)
        try:
            keys = sorted(value, key=_order)
        except (AttributeError, UnicodeError):
            raise Refusal("invalid_json", path) from None
        return {k: _ordered(value[k], _path(path, k), depth + 1) for k in keys}
    elif value is not None and type(value) is not bool:
        raise Refusal("invalid_json", path)
    return value


def canonical(value, path=""):
    return json.dumps(_ordered(value, path), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def content_identity(spec, version):
    raw = canonical(spec, "/spec")
    return {"version": version, "bytes": len(raw), "sha256": "c1:" + hashlib.sha256(raw).hexdigest()}


def _object(value, path, allowed, required=(), *, unknown="unknown_key"):
    if not isinstance(value, dict):
        raise Refusal("invalid_object", path)
    unique_object(value, path)
    for key in sorted(value.keys() - set(allowed), key=_order):
        raise Refusal(unknown, _path(path, key))
    for key in sorted(set(required) - value.keys(), key=_order):
        raise Refusal("missing_key", _path(path, key))


def _id(name):
    return isinstance(name, str) and _ID.fullmatch(name) is not None


def _fits(value, rule):
    types = {"string": str, "boolean": bool, "integer": int}
    kind = rule.get("type")
    if kind and type(value) is not types[kind]:
        return False
    if "enum" in rule and not any(type(v) is type(value) and v == value for v in rule["enum"]):
        return False
    return not isinstance(value, str) or rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", len(value))


def _schema(schema):
    path = "/answer_schema"
    _object(schema, path, ("type", "properties", "required"), ("type", "properties"), unknown="schema_keyword")
    props, required = schema["properties"], schema.get("required", [])
    if schema["type"] != "object" or not isinstance(props, dict):
        raise Refusal("schema_shape", path)
    if (not isinstance(required, list) or any(not _id(k) or k not in props for k in required)
            or len(set(required)) != len(required)):
        raise Refusal("schema_shape", path + "/required")
    for key in sorted(props, key=_order):
        p = _path(path + "/properties", key)
        if not _id(key) or key == "on":
            raise Refusal("schema_shape", p)
        rule = props[key]
        _object(rule, p, ("type", "enum", "minLength", "maxLength"), unknown="schema_keyword")
        kind, enum = rule.get("type"), rule.get("enum")
        if ("type" in rule and kind not in ("string", "boolean", "integer")) or (not kind and enum is None):
            raise Refusal("schema_shape", p)
        for bound in ("minLength", "maxLength"):
            if bound in rule and (kind != "string" or type(rule[bound]) is not int or rule[bound] < 0):
                raise Refusal("schema_shape", _path(p, bound))
        if "maxLength" in rule and rule.get("minLength", 0) > rule["maxLength"]:
            raise Refusal("schema_shape", p)
        if "enum" in rule:
            if (not isinstance(enum, list) or not enum
                    or any(type(v) not in (str, bool, int) for v in enum)
                    or len({(type(v), v) for v in enum}) != len(enum)
                    or any(not _fits(v, {k: val for k, val in rule.items() if k != "enum"}) for v in enum)):
                raise Refusal("schema_shape", _path(p, "enum"))


def _state(value, schema, path, *, submitted):
    _object(value, path, schema["properties"], schema.get("required", []) if submitted else ())
    for key in sorted(value, key=_order):
        if not _fits(value[key], schema["properties"][key]):
            raise Refusal("state_value", _path(path, key))


def _literal(value, rule, path):
    """Check literal props against the shipped catalog's closed vocabulary."""
    kind = rule.get("type")
    if kind == "object":
        _object(value, path, rule["properties"], rule.get("required", []))
        for key in sorted(value, key=_order):
            _literal(value[key], rule["properties"][key], _path(path, key))
    elif kind == "array":
        if not isinstance(value, list) or not rule["minItems"] <= len(value) <= rule["maxItems"]:
            raise Refusal("prop", path)
        for i, item in enumerate(value):
            _literal(item, rule["items"], _path(path, i))
    elif not _fits(value, rule) or (isinstance(value, str) and strip_hidden_blocks(value) != value):
        raise Refusal("prop", path)


def _expression(value, operator, path, fields):
    if not isinstance(value, dict) or set(value) != {operator}:
        raise Refusal("expression", path)
    pointer = value[operator]
    if (not isinstance(pointer, str) or not pointer.startswith("/") or not _id(pointer[1:])
            or pointer[1:] not in fields):
        raise Refusal("expression", path)
    return pointer[1:]


def _elements(spec, schema):
    nodes, root = spec["elements"], spec["root"]
    path = "/spec/elements"
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 200:
        raise Refusal("element_count", path)
    if not _id(root) or root not in nodes:
        raise Refusal("element_id", "/spec/root")
    bound, fields = set(), schema["properties"]
    for ident in sorted(nodes, key=_order):
        p = _path(path, ident)
        if not _id(ident) or ident == "on":
            raise Refusal("element_id", p)
        node = nodes[ident]
        _object(node, p, ("type", "props", "children"), ("type", "props"))
        kind = node["type"]
        if not isinstance(kind, str) or kind not in _CATALOG["components"]:
            raise Refusal("component", p + "/type")
        component = _CATALOG["components"][kind]
        children = node.get("children", [])
        if not isinstance(children, list) or (children and not component["children"]):
            raise Refusal("element_tree", p + "/children")
        for i, child in enumerate(children):
            if not _id(child) or child not in nodes:
                raise Refusal("element_id", _path(p + "/children", i))
        props, rules, pp = node["props"], component["props"], p + "/props"
        _object(props, pp, rules["properties"], rules.get("required", []))
        for key in sorted(props, key=_order):
            v, prop_path = props[key], _path(pp, key)
            if key in component.get("bindings", {}):
                state_key = _expression(v, "$bindState", prop_path, fields)
                rule = fields[state_key]
                match = component["bindings"][key]
                if (state_key in bound or (match == "string_enum_in_option_order" and
                        ("enum" not in rule or any(not isinstance(x, str) for x in rule["enum"])))
                        or (match != "string_enum_in_option_order" and rule.get("type") != match)):
                    raise Refusal("expression", prop_path)
                bound.add(state_key)
            elif key in component.get("reads", {}) and isinstance(v, dict):
                _expression(v, "$state", prop_path, fields)
            else:
                _literal(v, rules["properties"][key], prop_path)
        if kind == "choice":
            expected = fields[props["value"]["$bindState"][1:]]["enum"]
            if [o["value"] for o in props["options"]] != expected:
                raise Refusal("expression", pp + "/options")
    seen = set()
    pending = [(root, 1, False)]
    while pending:
        ident, level, in_card = pending.pop()
        p = _path(path, ident)
        if ident in seen or (in_card and nodes[ident]["type"] == "card"):
            raise Refusal("element_tree", p)
        if level > 12:
            raise Refusal("depth", p)
        seen.add(ident)
        pending.extend((child, level+1, in_card or nodes[ident]["type"] == "card")
                       for child in reversed(nodes[ident].get("children", [])))
    if len(seen) != len(nodes):
        raise Refusal("element_tree", path)


def validate(payload):
    fields = ("version", "catalog", "catalog_version", "spec", "submit", "answer_schema")
    _object(payload, "", fields, fields)
    if len(canonical(payload)) > 131072:
        raise Refusal("payload_size", "")
    if payload["catalog"] != _CATALOG["catalog"]:
        raise Refusal("unknown_catalog", "/catalog")
    if type(payload["catalog_version"]) is not int or payload["catalog_version"] != _CATALOG["catalog_version"]:
        raise Refusal("catalog_version", "/catalog_version")
    version = payload["version"]
    if not isinstance(version, str) or not 1 <= len(version) <= 80:
        raise Refusal("invalid_version", "/version")
    spec = payload["spec"]
    if len(canonical(spec)) > 32768:
        raise Refusal("spec_size", "/spec")
    check_unique(spec, "/spec", reject_on=True)
    _object(spec, "/spec", ("root", "elements", "state"), ("root", "elements", "state"))
    if len(canonical(spec["state"])) > 4096:
        raise Refusal("state_size", "/spec/state")
    schema = payload["answer_schema"]
    _schema(schema)
    _elements(spec, schema)
    _state(spec["state"], schema, "/spec/state", submitted=False)
    submit = payload["submit"]
    _object(submit, "/submit", ("intent", "label"), ("intent", "label"))
    if not _id(submit["intent"]):
        raise Refusal("submit_intent", "/submit/intent")
    if not isinstance(submit["label"], str) or not 1 <= len(submit["label"]) <= 40:
        raise Refusal("submit_label", "/submit/label")


def admit(artifact_id, payload, status, data, previous):
    _object(data, "", ("submission_id", "version", "answers", "synthesize_audio"),
            ("submission_id", "version", "answers"))
    ident = data["submission_id"]
    if not isinstance(ident, str) or not _SUBMISSION.fullmatch(ident):
        raise Refusal("invalid_submission_id", "/submission_id")
    answers = data["answers"]
    if len(canonical(answers, "/answers")) > 16384:
        raise Refusal("answer_size", "/answers")
    _object(answers, "/answers", ("intent", "state"), ("intent", "state"))
    audio = data.get("synthesize_audio", True)
    if type(audio) is not bool:
        raise Refusal("audio_preference", "/synthesize_audio")
    digest = hashlib.sha256(canonical([artifact_id, data["version"], answers])).hexdigest()
    if previous is not None:
        if previous["payload_hash"] != digest:
            raise Refusal("reused_id", "/submission_id")
        if "synthesize_audio" in data and bool(previous["synthesize_audio"]) != audio:
            raise Refusal("reused_id_audio", "/synthesize_audio")
        return {"repeat": True}
    if status == "completed":
        raise Refusal("already_answered", "")
    if status not in ("ready", "active"):
        raise Refusal("not_open", "")
    if data["version"] != payload["version"]:
        raise Refusal("version_mismatch", "/version")
    if answers["intent"] != payload["submit"]["intent"]:
        raise Refusal("intent_mismatch", "/answers/intent")
    _state(answers["state"], payload["answer_schema"], "/answers/state", submitted=True)
    return {"repeat": False, "payload_hash": digest, "answers_json": canonical(answers).decode()}


def message_text(artifact, submission):
    return (f"The user answered {artifact['title']!r}.\nArtifact ID: {artifact['artifact_id']}\n"
            f"Version: {submission['version']}\nSubmission ID: {submission['submission_id']}\n"
            "These are user preferences for review, not approval to perform protected actions.\n"
            "Answers:\n" + canonical(submission["answers"]).decode())
