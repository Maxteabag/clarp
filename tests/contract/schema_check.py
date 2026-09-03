"""Minimal JSON-Schema-subset validator for the contract tests.

Supports exactly what contract/schemas uses: type (string or list),
required, properties, items, enum, const, and local $ref (#/$defs/...,
#). additionalProperties is always allowed per the additive-only policy,
unknown keywords are ignored. Deliberately dependency-free so the
conformance test runs with the repo's existing pytest setup.
"""
from __future__ import annotations


class SchemaError(AssertionError):
    pass


def _resolve(root: dict, ref: str) -> dict:
    assert ref.startswith("#"), f"only local refs supported: {ref}"
    node: object = root
    for part in ref[1:].split("/"):
        if not part:
            continue
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"unresolvable $ref: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise SchemaError(f"$ref does not point at a schema: {ref}")
    return node


def _check_type(value: object, expected: object, path: str) -> None:
    names = [expected] if isinstance(expected, str) else list(expected or [])
    if not names:
        return
    ok = False
    for name in names:
        if name == "object" and isinstance(value, dict):
            ok = True
        elif name == "array" and isinstance(value, list):
            ok = True
        elif name == "string" and isinstance(value, str):
            ok = True
        elif name == "integer" and isinstance(value, bool):
            ok = False
        elif name == "integer" and isinstance(value, int):
            ok = True
        elif name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            ok = True
        elif name == "boolean" and isinstance(value, bool):
            ok = True
        elif name == "null" and value is None:
            ok = True
    if not ok:
        raise SchemaError(f"{path}: expected {expected}, got {value!r:.120}")


def validate(value: object, schema: dict, root: dict | None = None, path: str = "$") -> None:
    """Assert value satisfies schema; raise SchemaError describing the first gap."""
    root = root if root is not None else schema
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: bad schema node {schema!r:.120}")
    if "$ref" in schema:
        validate(value, _resolve(root, schema["$ref"]), root, path)
        return
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected const {schema['const']!r}, got {value!r:.120}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r:.120} not in enum {schema['enum']!r}")
    if "type" in schema:
        _check_type(value, schema["type"], path)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required field {key!r}")
        for key, subschema in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], subschema, root, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], root, f"{path}[{i}]")
