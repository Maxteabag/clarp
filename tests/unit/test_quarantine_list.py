"""The refactor program's flake quarantine names real tests, says why, and never
hides a test from CI."""
import pathlib
import re

import quarantine

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_entry_names_an_existing_test_and_an_env_or_load_reason():
    entries = quarantine.load()
    assert entries
    for node, note in entries.items():
        path, _, name = node.partition("::")
        function = re.sub(r"\[.*\]$", "", name)
        assert note.split(":")[0] in ("env", "load"), node
        assert re.search(rf"^\s*(async )?def {re.escape(function)}\(",
                         (ROOT / path).read_text(), re.M), node


def test_ci_and_the_override_run_quarantined_tests():
    assert quarantine.active({})
    assert not quarantine.active({"CI": "true"})
    assert not quarantine.active({"CLARP_RUN_QUARANTINED": "1"})
