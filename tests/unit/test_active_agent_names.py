"""The names given to the speech engine as 'active agent names' (R109).

They were every agent not deleted, archived ones included, oldest first, and the 600-character
budget then cut the oldest: on a Host with hundreds of archived agents the engine was primed mostly
with names nobody can address. Now: archived agents are out; the user's own agents come before
helpers, the most recently used first, ties by creation; the budget cuts from the end."""
from __future__ import annotations

import pytest

from lib import agents, db, vocab
from lib.vocab_service import VocabService

LIVE_OWN, LIVE_HELPERS, ARCHIVED = 40, 20, 300


def _agent(name, *, helper=False, archived=False, created_at=0):
    agent_id = agents.create_agent(persona=name, voice_id="v", cwd="/tmp", session=name.lower())
    db.conn().execute("UPDATE agents SET created_at=?, role=?, parent_agent_id=?, archived_at=? WHERE agent_id=?",
                      (created_at, "helper" if helper else "agent", "parent-id" if helper else None,
                       1_000 if archived else None, agent_id))
    return agent_id


def _used(agent_id, at):
    db.conn().execute("INSERT INTO turns(agent_id, source, trace_id, started_at) VALUES(?,?,?,?)",
                      (agent_id, "pwa", f"trace-{agent_id}-{at}", at))


@pytest.fixture
def fleet():
    ids = {}
    for n in range(ARCHIVED):                       # created first: the oldest
        ids[f"Archived-agent-number-{n:03d}"] = _agent(f"Archived-agent-number-{n:03d}", archived=True,
                                                         created_at=1_000 + n)
    for n in range(LIVE_OWN):
        ids[f"Own-agent-number-{n:02d}"] = _agent(f"Own-agent-number-{n:02d}", created_at=10_000 + n)
    for n in range(LIVE_HELPERS):
        ids[f"Helper-number-{n:02d}"] = _agent(f"Helper-number-{n:02d}", helper=True, created_at=20_000 + n)
    return ids


def _names():
    return VocabService(lambda: None).active_agent_names()


def test_no_archived_agent_is_an_active_agent(fleet):
    names = _names()
    assert len(names) == LIVE_OWN + LIVE_HELPERS
    assert not [n for n in names if n.startswith("Archived")]


def test_the_users_own_agents_come_before_helpers(fleet):
    names = _names()
    assert all(n.startswith("Own") for n in names[:LIVE_OWN])
    assert all(n.startswith("Helper") for n in names[LIVE_OWN:])


def test_the_most_recently_used_come_first_and_ties_by_creation(fleet):
    _used(fleet["Own-agent-number-07"], 9_000)
    _used(fleet["Own-agent-number-03"], 5_000)
    _used(fleet["Own-agent-number-03"], 2_000)          # its latest turn counts
    _used(fleet["Helper-number-05"], 9_999)             # a helper stays after every own agent
    names = _names()
    assert names[:3] == ["Own-agent-number-07", "Own-agent-number-03", "Own-agent-number-00"]
    assert names[LIVE_OWN] == "Helper-number-05"


def test_the_list_is_stable_from_one_call_to_the_next(fleet):
    _used(fleet["Own-agent-number-11"], 7_000)
    assert _names() == _names() == _names()


def test_the_budget_keeps_the_likeliest_names(fleet):
    _used(fleet["Own-agent-number-31"], 9_000)
    prompt = vocab.build_initial_prompt("", _names(), include_agent_names=True,
                                        include_technical_glossary=False)
    assert len(prompt) <= vocab.MAX_PROMPT_CHARS
    assert prompt.startswith("Agent names: Own-agent-number-31, Own-agent-number-00")
    assert "Archived" not in prompt and "Helper" not in prompt     # cut from the end


def test_transcription_guidance_answers_only_active_names(fleet):
    payload = vocab.settings_payload(_names())
    assert not [n for n in payload["active_agent_names"] if n.startswith("Archived")]
    assert "Archived" not in payload["delegation_effective_prompt"]


# The cut (R109, ruled): a name that does not fit in what is left is skipped, the walk goes on.

LONG = "Overlong-agent-name-" * 5                     # 100 characters: never fits a 40-character budget


def _prompt(names, budget):
    return vocab.build_initial_prompt("", names, include_agent_names=True,
                                      include_technical_glossary=False, max_prompt_chars=budget)


@pytest.mark.parametrize("names", [[LONG, "Ann", "Bob"], ["Ann", LONG, "Bob"], ["Ann", "Bob", LONG]],
                         ids=["first", "in the middle", "last"])
def test_an_over_long_name_is_skipped_wherever_it_stands(names):
    assert _prompt(names, 40) == "Agent names: Ann, Bob."
    assert _prompt(names, 40) == _prompt(names, 40)


def test_every_name_over_long_gives_no_names_and_no_error():
    assert _prompt([LONG, LONG + "2", LONG + "3"], 40) == ""


def test_the_budget_is_characters_with_prefix_separators_and_full_stop_and_may_be_reached_exactly():
    exact = "Agent names: Ann, Bob."
    assert _prompt(["Ann", "Bob", "Cy"], len(exact)) == exact            # 22 characters: Cy skipped
    assert _prompt(["Ann", "Bob", "Cy"], len(exact) - 1) == "Agent names: Ann, Cy."
    assert _prompt(["Ann", "Bob", "Cy"], len(exact) + 4) == "Agent names: Ann, Bob, Cy."


# The engine (R109, Maximus's reading): /transcribe hands the FULL ordered list to the compile,
# which makes the 'agents' pack of it and cuts it to the provider's budget (vocab_budget.select).

def test_the_engine_keeps_the_first_names_of_the_ordered_list_when_the_fleet_is_too_big(fleet):
    """A CamelCase helper, created last and never used, is last in the order; its shape once rated
    it rarer than the others, so the compile put it first. Now every agent name scores alike."""
    _agent("DevBot", helper=True, created_at=30_000)
    _agent("sol_helper", helper=True, created_at=30_001)
    _used(fleet["Own-agent-number-31"], 9_000)
    ordered = _names()
    assert ordered[0] == "Own-agent-number-31" and ordered[-2:] == ["DevBot", "sol_helper"]
    payload = VocabService(lambda: None).for_transcription(
        delegated=True, requested_model="faster-whisper").payload
    sent = [term.strip(" .") for term in payload.split(",")]
    picked = [name for name in reversed(sent) if name in ordered]      # rendered best last
    assert 2 <= len(picked) < len(ordered)                             # the budget did cut
    assert picked == [name for name in ordered if name in picked]      # the order kept
    assert picked[0] == "Own-agent-number-31"                          # the likeliest first
    assert "DevBot" not in picked and "sol_helper" not in picked
    # A name before the last one taken is left out only because it did not fit what was left:
    # it costs more than every name taken after it (the budget's skip rule, vocab_budget.select).
    from lib.vocab_budget import estimate_tokens
    last = ordered.index(picked[-1])
    for position, name in enumerate(ordered[:last]):
        if name not in picked:
            later = [n for n in ordered[position + 1:last + 1] if n in picked]
            assert estimate_tokens(name) > max(estimate_tokens(n) for n in later)


# The compile's cut, pinned for each budget kind (R109 (a), ruled): every agent name scores the
# same, so the order given is the order chosen, and the cut takes the first names that fit.

from lib import vocab_compile                                   # noqa: E402
from lib.vocab_budget import Budget, Pack, Term, Unit, compile_packs   # noqa: E402

ORDERED = ("Theo", "Arnold", "Maximus", "Nori", "hazel", "Rachel", "Knut Thomas", "Bea", "Ola",
           "DevBot", "sol_helper")                      # the two identifier-shaped names last


def _agent_terms(result):
    return [t.text for t in result.terms if t.pack == "agents"]


@pytest.mark.parametrize("budget", [Budget("p", "m", Unit.TOKENS, 20), Budget("p", "m", Unit.TERMS, 5),
                                    Budget("p", "m", Unit.WORDS, 6)], ids=["tokens", "terms", "words"])
def test_each_budget_kind_keeps_the_first_names_of_the_order(budget):
    result = compile_packs(vocab_compile.dynamic_packs(vocab_compile.Sources(agent_names=ORDERED)), budget)
    picked = _agent_terms(result)
    assert 2 <= len(picked) < len(ORDERED)
    assert picked == list(ORDERED[:len(picked)])
    assert "DevBot" not in picked and "sol_helper" not in picked


def test_every_agent_name_has_the_same_score():
    terms = vocab_compile.dynamic_packs(vocab_compile.Sources(agent_names=ORDERED))[0].terms
    assert {t.rarity for t in terms} == {0.9} and [t.text for t in terms] == list(ORDERED)


def test_the_floor_keeps_the_first_two_names_of_the_order():
    """A pack that outscores every agent name takes the rest of the budget; the agents pack's
    floor of 2 still ships two names: the first two of the order, equal scores broken by the
    order given (the sort is stable)."""
    rival = Pack(name="rival", priority=3.0,
                 terms=tuple(Term(text=f"RivalTerm{n}", pack="rival", rarity=0.98) for n in range(20)))
    packs = [rival, *vocab_compile.dynamic_packs(vocab_compile.Sources(agent_names=ORDERED[::-1]))]
    result = compile_packs(packs, Budget("p", "m", Unit.TERMS, 4))
    assert _agent_terms(result) == ["sol_helper", "DevBot"]            # the first two of THIS order
    assert len(result.terms) == 4
