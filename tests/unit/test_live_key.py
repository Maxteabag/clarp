"""The GPT-Live lab scripts take the OpenAI key only from an OPENAI_ variable (issue 473, review G7)."""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("live_key", SCRIPTS / "live_key.py")
live_key = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_key)


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENAI_LAB_KEY", "OPENAI_2"])
def test_an_openai_variable_is_taken(name):
    assert live_key.key_env_name(name) == name


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_", "OPENAI", "openai_api_key",
                                  "MY_OPENAI_API_KEY", "OPENAI_API_KEY ", "OPENAI-API-KEY", "OPENAI_KEY;x", ""])
def test_any_other_name_is_refused_in_words(name):
    with pytest.raises(argparse.ArgumentTypeError, match="refused: .* starts with OPENAI_"):
        live_key.key_env_name(name)


def test_a_refused_name_is_never_read(monkeypatch):
    class Environment(dict):
        def get(self, *args):
            raise AssertionError("an environment variable was read")
    monkeypatch.setattr(live_key.os, "environ", Environment())
    with pytest.raises(argparse.ArgumentTypeError):
        live_key.key_from("GITHUB_TOKEN")


def test_the_key_is_read_from_its_variable_and_scrubbed(monkeypatch):
    monkeypatch.setenv("OPENAI_LAB_KEY", " sk-lab ")
    assert live_key.key_from("OPENAI_LAB_KEY") == "sk-lab"
    assert live_key.scrub("Bearer sk-lab", "sk-lab") == "Bearer [key]"


@pytest.mark.parametrize("script", ["live_standby_walk.py", "live_norwegian_voices.py", "live_socket_standby_probe.py"])
def test_every_lab_script_checks_the_name_when_its_arguments_are_read(script):
    text = (SCRIPTS / script).read_text()
    assert re.search(r'"--key-env", default="OPENAI_API_KEY", type=key_env_name', text)
    assert "from live_key import key_env_name, key_from, scrub" in text
    assert "os.environ" not in text and "def key_from" not in text
