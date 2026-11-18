"""The render_spec v1 contract. Verdicts depend only on the submitted JSON and the
catalog file, which is read on first use by a render_spec request, never at import:
a missing or unreadable catalog refuses render_spec alone and no other artifact kind."""
from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path
from typing import NamedTuple

from .voice_markup import strip_hidden_blocks

_SUBMISSION = re.compile(r"[A-Za-z0-9-]{16,80}\Z")
MAX_INTEGER = 2**53 - 1
CATALOG_UNAVAILABLE = "catalog_unavailable"
# The one refusal whose sentence cannot come from the refusals file: it is the same
# sentence as the file's (a test holds them equal).
CATALOG_UNAVAILABLE_SENTENCE = "The form catalog is not available on this Host."
# Every code this module and its callers refuse with; the refusals file must give each a sentence.
CODES = frozenset({
    "client_contract", "invalid_object", "unknown_key", "missing_key", "invalid_json", "payload_size",
    "unknown_catalog", "catalog_version", "invalid_version", "spec_size", "state_size", "answer_size",
    "element_count", "element_id", "element_tree", "depth", "component", "prop", "expression",
    "submit_intent", "submit_label", "schema_keyword", "schema_shape", "state_value", "intent_mismatch",
    "invalid_submission_id", "audio_preference", "reused_id", "reused_id_audio", "version_mismatch",
    "already_answered", "not_open", "number_form", CATALOG_UNAVAILABLE})
# Keys the contract itself names. A pointer names these, array indexes and keys that pass
# the identifier rule; at any other key it stops at the parent (in the response and the log).
STRUCTURAL_KEYS = frozenset({
    "type", "title", "summary", "version", "catalog", "catalog_version", "spec", "submit", "answer_schema",
    "root", "elements", "state", "props", "children", "properties", "required", "enum", "minLength",
    "maxLength", "intent", "label", "submission_id", "answers", "synthesize_audio", "$state", "$bindState"})
# The catalog's own schema language. A catalog file that uses another keyword is not loaded.
SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "const",
                             "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"})
_TYPES = {"string": str, "boolean": bool, "integer": int, "object": dict, "array": list}
# What a catalog binding names: the bound key's declared type, or this rule, which the code
# defines: the key is an enum of strings equal to the values of the control's `options`, in order.
OPTION_ORDER = "string_enum_in_option_order"
_BINDINGS = frozenset({"string", "boolean", "integer", OPTION_ORDER})


def _catalog_dir() -> Path:
    here = Path(__file__).resolve().parents[1] / "contract/catalogs"
    # A source checkout keeps contract/ at the repository root; an install beside lib/.
    return here if here.is_dir() else here.parents[2] / "contract/catalogs"


class Catalog(NamedTuple):
    raw: bytes
    data: dict
    reasons: dict
    sha256: str
    ident: re.Pattern


def _schemas(rule, found):
    """Every schema object inside a catalog schema, for the keyword check."""
    if not isinstance(rule, dict):
        raise ValueError("catalog schema")
    found.append(rule)
    for child in (rule.get("properties") or {}).values():
        _schemas(child, found)
    if "items" in rule:
        _schemas(rule["items"], found)
    return found


def _check_catalog(data, reasons):
    named = set(data["schema_keywords"])
    if not named <= SCHEMA_KEYWORDS or not CODES <= reasons.keys():
        raise ValueError("catalog keywords or refusal codes")
    shapes = data["expression_shapes"]
    for shape in shapes.values():
        if len(shape.get("required", ())) != 1:
            raise ValueError("expression shape")
    found = [rule for shape in shapes.values() for rule in _schemas(shape, [])]
    for name, component in data["components"].items():
        props = component["props"]
        found += _schemas(props, [])
        declared = props.get("properties") or {}
        if (type(component["children"]) is not bool
                or not set(component.get("forbid_descendants", ())) <= data["components"].keys()
                or any(k not in declared or v not in _BINDINGS for k, v in component.get("bindings", {}).items())
                or any(k not in declared or v not in shapes for k, v in component.get("reads", {}).items())):
            raise ValueError("catalog component")
        for key in component.get("bindings", {}):
            if len(declared[key].get("required", ())) != 1:
                raise ValueError("binding shape")
    for rule in found:
        if not rule.keys() <= named or rule.get("type", "string") not in _TYPES:
            raise ValueError("catalog keyword")
    rule = data["identifiers"]
    if not 1 <= rule["minLength"] <= rule["maxLength"] or not rule["first"] or not rule["rest"]:
        raise ValueError("identifier rule")


@functools.lru_cache(maxsize=1)
def _loaded() -> Catalog:
    folder = _catalog_dir()
    raw = (folder / "clarp.form.v1.json").read_bytes()
    data, reasons = json.loads(raw), json.loads((folder / "clarp.form.v1.refusals.json").read_bytes())
    _check_catalog(data, reasons)
    rule = data["identifiers"]
    first, rest = ("".join(map(re.escape, rule[k])) for k in ("first", "rest"))
    ident = re.compile(f"[{first}][{rest}]{{{rule['minLength'] - 1},{rule['maxLength'] - 1}}}\\Z")
    return Catalog(raw, data, reasons, "c1:" + hashlib.sha256(raw).hexdigest(), ident)


def catalog() -> Catalog:
    """The catalog, read once; CatalogUnavailable while it cannot be read (retried on the next use)."""
    try:
        return _loaded()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error):
        raise CatalogUnavailable() from None


def __getattr__(name):
    if name == "CATALOG_BYTES":
        return catalog().raw
    if name == "CATALOG_SHA256":
        return catalog().sha256
    raise AttributeError(name)


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
        if self.code != CATALOG_UNAVAILABLE:
            try:
                return {"code": self.code, "path": self.path, "message": catalog().reasons[self.code]}
            except CatalogUnavailable:
                pass
        return {"code": CATALOG_UNAVAILABLE, "path": "", "message": CATALOG_UNAVAILABLE_SENTENCE}


class CatalogUnavailable(Refusal):
    def __init__(self):
        super().__init__(CATALOG_UNAVAILABLE, "")


def _path(path, key):
    """A pointer segment: an array index, a key the contract names, or a valid identifier.
    Any other key (a caller's invalid id or key) is never named: the pointer stays at the parent."""
    if type(key) is int or (isinstance(key, str) and (key in STRUCTURAL_KEYS or _named(key))):
        return f"{path}/{key}"
    return path


def _named(key):
    try:
        return catalog().ident.fullmatch(key) is not None
    except CatalogUnavailable:
        return False


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
    return isinstance(name, str) and catalog().ident.fullmatch(name) is not None


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


def _same(value, member):
    return type(value) is type(member) and value == member


def _literal(value, rule, path):
    """A literal value against its catalog schema, in the catalog's own keywords."""
    kind = rule.get("type")
    if kind == "object" or (kind is None and "properties" in rule):
        declared = rule.get("properties", {})
        closed = rule.get("additionalProperties", True) is False
        _object(value, path, declared if closed else value.keys() | declared.keys() if isinstance(value, dict)
                else declared, rule.get("required", []))
        for key in sorted(value, key=_order):
            if key in declared:
                _literal(value[key], declared[key], _path(path, key))
        return
    if kind is not None and type(value) is not _TYPES[kind]:
        raise Refusal("prop", path)
    if ("const" in rule and not _same(value, rule["const"])) or (
            "enum" in rule and not any(_same(value, member) for member in rule["enum"])):
        raise Refusal("prop", path)
    if isinstance(value, str) and (not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", len(value))
                                   or strip_hidden_blocks(value) != value):
        raise Refusal("prop", path)
    if type(value) is int and not rule.get("minimum", value) <= value <= rule.get("maximum", value):
        raise Refusal("prop", path)
    if isinstance(value, list):
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", len(value)):
            raise Refusal("prop", path)
        for i, item in enumerate(value):
            if "items" in rule:
                _literal(item, rule["items"], _path(path, i))


def _expression(value, shape, path, fields):
    """A state expression in the shape the catalog gives it; its one key is the operator.
    The pointer is code: "/" and an answer_schema key, one segment."""
    try:
        _literal(value, shape, path)
    except Refusal:
        raise Refusal("expression", path) from None
    [operator] = shape["required"]
    pointer = value[operator]
    if (not isinstance(pointer, str) or not pointer.startswith("/") or not _id(pointer[1:])
            or pointer[1:] not in fields):
        raise Refusal("expression", path)
    return pointer[1:]


def _binds(match, rule):
    if match == OPTION_ORDER:
        return "enum" in rule and all(isinstance(x, str) for x in rule["enum"])
    return rule.get("type") == match


def _elements(spec, schema):
    nodes, root = spec["elements"], spec["root"]
    path = "/spec/elements"
    components, shapes = catalog().data["components"], catalog().data["expression_shapes"]
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
        if not isinstance(kind, str) or kind not in components:
            raise Refusal("component", p + "/type")
        component = components[kind]
        children = node.get("children", [])
        if not isinstance(children, list) or (children and not component["children"]):
            raise Refusal("element_tree", p + "/children")
        for i, child in enumerate(children):
            if not _id(child) or child not in nodes:
                raise Refusal("element_id", _path(p + "/children", i))
        props, rules, pp = node["props"], component["props"], p + "/props"
        declared = rules.get("properties", {})
        closed = rules.get("additionalProperties", True) is False
        _object(props, pp, declared if closed else props.keys() | declared.keys() if isinstance(props, dict)
                else declared, rules.get("required", []))
        bindings, reads = component.get("bindings", {}), component.get("reads", {})
        for key in sorted(props, key=_order):
            if key not in declared:
                continue
            v, prop_path = props[key], _path(pp, key)
            if key in bindings:
                state_key = _expression(v, declared[key], prop_path, fields)
                if state_key in bound or not _binds(bindings[key], fields[state_key]):
                    raise Refusal("expression", prop_path)
                bound.add(state_key)
            elif key in reads and isinstance(v, dict):
                _expression(v, shapes[reads[key]], prop_path, fields)
            else:
                _literal(v, declared[key], prop_path)
        for key, match in sorted(bindings.items()):
            if match == OPTION_ORDER and key in props:
                [operator] = declared[key]["required"]
                expected = fields[props[key][operator][1:]]["enum"]
                options = props.get("options")
                if not isinstance(options, list) or [o.get("value") if isinstance(o, dict) else None
                                                     for o in options] != expected:
                    raise Refusal("expression", pp + "/options")
    seen = set()
    pending = [(root, 1, frozenset())]
    while pending:
        ident, level, forbidden = pending.pop()
        p = _path(path, ident)
        kind = nodes[ident]["type"]
        if ident in seen or kind in forbidden:
            raise Refusal("element_tree", p)
        if level > 12:
            raise Refusal("depth", p)
        seen.add(ident)
        below = forbidden | frozenset(components[kind].get("forbid_descendants", ()))
        pending.extend((child, level+1, below) for child in reversed(nodes[ident].get("children", [])))
    if len(seen) != len(nodes):
        raise Refusal("element_tree", path)


def validate(payload):
    known = catalog().data
    fields = ("version", "catalog", "catalog_version", "spec", "submit", "answer_schema")
    _object(payload, "", fields, fields)
    if len(canonical(payload)) > 131072:
        raise Refusal("payload_size", "")
    if payload["catalog"] != known["catalog"]:
        raise Refusal("unknown_catalog", "/catalog")
    if type(payload["catalog_version"]) is not int or payload["catalog_version"] != known["catalog_version"]:
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
    catalog()
    _object(data, "", ("submission_id", "version", "answers", "synthesize_audio"),
            ("submission_id", "version", "answers"))
    ident = data["submission_id"]
    if not isinstance(ident, str) or not _SUBMISSION.fullmatch(ident):
        raise Refusal("invalid_submission_id", "/submission_id")
    if not isinstance(data["version"], str):  # before anything hashes it
        raise Refusal("invalid_version", "/version")
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
    # The title as a canonical JSON string (RFC 8785), so every Host writes the same bytes.
    return (f"The user answered {canonical(artifact['title']).decode()}.\nArtifact ID: {artifact['artifact_id']}\n"
            f"Version: {submission['version']}\nSubmission ID: {submission['submission_id']}\n"
            "These are user preferences for review, not approval to perform protected actions.\n"
            "Answers:\n" + canonical(submission["answers"]).decode())
