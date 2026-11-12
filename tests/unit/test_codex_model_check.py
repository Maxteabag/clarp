"""Codex refuses a model id its own catalogue does not list.

laptopstudio, 2026-11-11: two helpers were started with model ``gpt-6``, an
id Codex does not have. Codex only says so when a turn runs ("The 'gpt-6'
model is not supported when using Codex with a ChatGPT account"), which reads
as an account's plan failing. The Host now refuses such an id where a model is
set, from the catalogue it already caches; it never probes for the check, and
without a list read from the CLI the id is accepted as before.
"""
from __future__ import annotations

import time

import pytest

from lib import backends, janitor_design_policy, janitors, provider_capabilities
from lib.policies import agent_spec
from tests.unit.test_policies_agent_spec import PERSONAS, VIEW, FakeBackends


def _observed_codex_models(monkeypatch, ids, *, kind="cli_probe"):
    """The Host's cached model catalogue, as its last look at Codex left it."""
    source = {"kind": kind, "detail": "codex debug models", "freshness": "fresh"}
    catalogue = {"providers": {"codex": {
        "id": "codex", "source": source,
        "models": [{"id": model, "label": model} for model in ids]}}}
    monkeypatch.setattr(provider_capabilities, "_cache",
                        (time.monotonic(), catalogue, ()))


def test_codex_refuses_a_model_id_its_catalogue_does_not_list(monkeypatch):
    _observed_codex_models(monkeypatch, ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"])

    assert backends.is_valid_model("codex", "gpt-6") is False
    assert backends.is_valid_model("codex", "gpt-6-sol") is True
    assert backends.is_valid_model("codex", " gpt-6.1-sol ") is True
    assert backends.is_valid_model("codex", "") is True          # the CLI's default
    assert backends.is_valid_model("codex", None) is True
    assert backends.known_models("codex") == ["gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol"]
    # Another backend is not judged by Codex's list.
    assert backends.is_valid_model("claude", "gpt-6") is True


@pytest.mark.parametrize("case", ["never-observed", "probe-failed", "empty-list"])
def test_without_an_observed_catalogue_codex_accepts_the_id_as_before(monkeypatch, case):
    """The check never guesses and never probes: with no list read from the
    CLI itself there is nothing to compare against, and the id is accepted."""
    if case == "never-observed":
        monkeypatch.setattr(provider_capabilities, "_cache", None)
    elif case == "probe-failed":      # the bundled fallback list is not the CLI's word
        _observed_codex_models(monkeypatch, ["gpt-6-sol"], kind="static_fallback")
    else:
        _observed_codex_models(monkeypatch, [])

    assert backends.is_valid_model("codex", "gpt-6") is True
    assert backends.known_models("codex") == []
    assert backends.unknown_model_detail("codex", "gpt-6") == ""


def test_starting_an_agent_with_an_unknown_model_is_refused_and_says_what_exists(monkeypatch):
    """The launch request is where a helper gets its model."""

    class RealModels(FakeBackends):
        is_valid_model = staticmethod(backends.is_valid_model)
        known_models = staticmethod(backends.known_models)

    _observed_codex_models(monkeypatch, ["gpt-6.1-sol", "gpt-6-sol"])

    refused = agent_spec.AgentSpec.parse(
        {"name": "Helper", "backend": "codex", "model": "gpt-6"},
        backends=RealModels(), roster=VIEW, personas=PERSONAS)
    accepted = agent_spec.AgentSpec.parse(
        {"name": "Helper", "backend": "codex", "model": "gpt-6-sol"},
        backends=RealModels(), roster=VIEW, personas=PERSONAS)

    assert isinstance(refused, agent_spec.SpecError)
    assert (refused.status, refused.message) == (400, "invalid model for backend")
    assert refused.detail == "codex has no model 'gpt-6'; it offers gpt-6-sol, gpt-6.1-sol"
    assert isinstance(accepted, agent_spec.AgentSpec) and accepted.model == "gpt-6-sol"


def test_a_janitors_model_is_refused_with_the_ids_codex_offers(monkeypatch):
    _observed_codex_models(monkeypatch, ["gpt-6.1-sol", "gpt-6-sol"])

    with pytest.raises(janitors.JanitorError) as refused:
        janitors._model({"backend": "codex"}, "gpt-6", None)
    assert str(refused.value) == (
        "Invalid model for this agent: codex has no model 'gpt-6'; "
        "it offers gpt-6-sol, gpt-6.1-sol")
    janitors._model({"backend": "codex"}, "gpt-6-sol", None)       # accepted

    with pytest.raises(ValueError) as chain:
        janitor_design_policy.configure(
            {"model_chain": [{"provider": "codex", "model": "gpt-6"}]}, 0)
    assert str(chain.value) == (
        "Invalid provider model identifier: codex has no model 'gpt-6'; "
        "it offers gpt-6-sol, gpt-6.1-sol")


def test_a_janitor_refusal_without_a_list_says_only_that_it_is_invalid(monkeypatch):
    monkeypatch.setattr(provider_capabilities, "_cache", None)

    with pytest.raises(janitors.JanitorError) as refused:
        janitors._model({"backend": "codex"}, "x" * 161, None)     # too long, whatever the list
    assert str(refused.value) == "Invalid model for this agent"
