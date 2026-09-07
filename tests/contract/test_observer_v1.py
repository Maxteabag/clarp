"""The observer projection is deliberately narrower than the full Host wire API."""
import copy
import json
from pathlib import Path
import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2] / "contract/observer-v1"
SCHEMA = json.loads((ROOT / "schema.json").read_text())
FIXTURES = json.loads((ROOT / "fixtures.json").read_text())

def test_observer_examples_are_valid():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    for case in FIXTURES["cases"]:
        jsonschema.validate(case["snapshot"], SCHEMA)
        for event in case["events"]:
            jsonschema.validate(event, SCHEMA)

def test_observer_rejects_private_fields_and_unsupported_versions():
    snapshot = copy.deepcopy(FIXTURES["cases"][0]["snapshot"])
    snapshot["agents"][0]["last_message"] = "must not cross the observer boundary"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(snapshot, SCHEMA)
    snapshot = copy.deepcopy(FIXTURES["cases"][0]["snapshot"])
    snapshot["version"] = 2
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(snapshot, SCHEMA)
