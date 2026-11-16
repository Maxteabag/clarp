"""B12, B13: spoken-name routing must work anywhere in the first three words,
and survive Whisper mishearing the name."""

from __future__ import annotations

import pytest

from lib.routing import (
    resolve_agent_by_spoken_name,
    word_similarity,
)


AGENTS = {
    "claude": {"name": "Mike",   "voice_id": "v-mike"},
    "arnold": {"name": "Arnold", "voice_id": "v-arnold"},
    "rachel": {"name": "Rachel", "voice_id": "v-rachel"},
    "domi":   {"name": "Domi",   "voice_id": "v-domi"},
    "bella":  {"name": "Bella",  "voice_id": "v-bella"},
}


@pytest.mark.parametrize("text,expected_session,forwarded", [
    ("Arnold, do X", "arnold", "do X"),
    ("also Arnold, do X", "arnold", "do X"),
    ("hey Bella fix the bug", "bella", "fix the bug"),
    ("Rachel, write me an essay", "rachel", "write me an essay"),
    ("Hi Rachel, what's up?", "rachel", "what's up?"),
    ("Hey Rachel can you check the logs", "rachel", "can you check the logs"),
    ("Yo, hey Mike how are things", "claude", "how are things"),
    # Name later than position 3 does NOT route.
    ("I will tell Rachel later about the bug", None, "I will tell Rachel later about the bug"),
    ("I was talking about Bella", None, "I was talking about Bella"),
    ("Yeah, go ahead and fix that, Bella.", None, "Yeah, go ahead and fix that, Bella."),
    ("just a normal sentence", None, "just a normal sentence"),
])
def test_exact_name_routing(text, expected_session, forwarded):
    """B12: route only prefix-addressed names and strip the address."""
    sid, returned = resolve_agent_by_spoken_name(text, AGENTS)
    assert sid == expected_session
    assert returned == forwarded


@pytest.mark.parametrize("misheard,expected_session", [
    # B13: Whisper-style clipped variants of female names.
    ("Bell, write me a poem",  "bella"),
    ("Bel, check the diff",    "bella"),
    ("Dom, look at this file", "domi"),
    ("Rach, what time is it?", "rachel"),
])
def test_fuzzy_routing_for_misheard_names(misheard, expected_session):
    sid, returned = resolve_agent_by_spoken_name(misheard, AGENTS)
    assert sid == expected_session
    assert returned


@pytest.mark.parametrize("text", [
    "Tell me how the weather is",
    "I just want a coffee",
    "what's the time",
])
def test_fuzzy_routing_does_not_misfire_on_ordinary_speech(text):
    sid, _ = resolve_agent_by_spoken_name(text, AGENTS)
    assert sid is None


def test_word_similarity_prefix_score_meets_threshold():
    # B13 specifically — the fuzzy threshold is 0.78. Ensure Bell→Bella clears.
    assert word_similarity("Bell", "Bella") >= 0.78
    # Whole different word stays below.
    assert word_similarity("Coffee", "Bella") < 0.5


@pytest.mark.parametrize("name,address,body", [
    ("Zoë", "Zoë, ", "check the branch"),
    ("Zoé", "hey Zoé: ", "check the branch"),
    ("Rachél", "Rachél, ", "check the branch"),
    ("Zoë", "Zoë", ""),
    ("Zoe\u0308", "Zoe\u0308, ", "check the branch"),
    ("Rache\u0301l", "hi Rache\u0301l! ", "check the branch"),
    ("Zoë\u0301", "Zoë\u0301; ", "check the branch"),
    ("Zoe\u0308\u0301", "Zoe\u0308\u0301? ", "check the branch"),
    ("Zoë", "Zoë, ", "écris à Rachél"),
    ("Zoë", "Zoë ", "\u0301keep this mark"),
    ("Zoë", "Zoë", "♥ check the branch"),
    ("Rachel", "Rachel", ""),
])
def test_unicode_address_is_removed_without_eating_the_request(name, address, body):
    assert resolve_agent_by_spoken_name(address + body, {"agent": {"name": name}}) == (
        "agent", body)


# Each row records the pre-387 resolver result and the wanted body. Put preserved
# request tails first: the first PR loop lost them despite choosing the same agent.
@pytest.mark.parametrize("names,text,before_387,wanted", [
    (("Koko",), "Koko请检查分支 now", ("target", "请检查分支 now"), "请检查分支 now"),
    (("Koko",), "Koko안녕 world", ("target", "안녕 world"), "안녕 world"),
    (("Koko",), "Koko请检查分支", ("target", "请检查分支"), "请检查分支"),
    (("Koko",), "Kokoさん、確認して", ("target", "さん、確認して"), "さん、確認して"),
    (("Koko",), "Koko안녕", ("target", "안녕"), "안녕"),
    (("Koko",), "Kokoёлка", ("target", "ёлка"), "ёлка"),
    (("Koko",), "Kokoß", ("target", "ß"), "ß"),
    (("Zoé",), "Zoécheck", ("target", "écheck"), "écheck"),
    (("Zoé",), "Zoécheck now", ("target", "écheck now"), "écheck now"),
    (("Koko",), "Koko—go", ("target", "—go"), "—go"),
    (("Koko",), "Koko’s branch", ("target", "’s branch"), "’s branch"),
    (("Zoe\u0308",), "Zoe\u0308, go", ("target", "\u0308, go"), "go"),
    (("Rachél",), "Rachél, go", ("target", "él, go"), "go"),
    (("Rache\u0301l",), "Rache\u0301l, go", ("target", "\u0301l, go"), "go"),
    (("Zoë",), "Zoë2 go", ("target", "ë2 go"), "ë2 go"),
    (("Zoë",), "Zoë_Marie go", ("target", "ë_Marie go"), "_Marie go"),
    (("Zoë",), "Zoë-Marie go", ("target", "ë-Marie go"), "Marie go"),
    (("Zoë-Marie",), "Zoë-Marie go", (None, "Zoë-Marie go"), "Zoë-Marie go"),
    (("Koko",), "Koko2 go", ("target", "go"), "go"),
    (("Koko",), "Koko_Marie go", ("target", "go"), "go"),
    (("Koko",), "Koko-Marie go", ("target", "go"), "go"),
    (("Koko",), "Koko, ask Zoë later", ("target", "ask Zoë later"), "ask Zoë later"),
    (("Zoë",), "Zoë go", ("target", "ë go"), "go"),
    (("Zoë",), "I will tell them about Zoë, go", (None, "I will tell them about Zoë, go"),
     "I will tell them about Zoë, go"),
    (("Zoë",), "Ask Zoë, go", ("target", "ë, go"), "go"),
    (("Zoë", "Zo"), "Zoë, go", ("target", "ë, go"), "go"),
    (("Zoë", "Zo"), "Zo, go", ("target", "go"), "go"),
    (("Zoë", "Zo"), "Zoé, go", ("target", "é, go"), "é, go"),
    (("Zoë", "Zo"), "hey Zoë, go", ("target", "ë, go"), "go"),
    # Accent insertions/substitutions are not the selected agent's exact name:
    # keep main's body rather than guess how far a misspelling extends.
    (("Rachel",), "Rachél, go", ("target", "él, go"), "él, go"),
    (("Rachel",), "Rache\u0301l, go", ("target", "\u0301l, go"), "\u0301l, go"),
    (("Zoế",), "Zoê\u0301check", ("target", "ê\u0301check"), "ê\u0301check"),
    (("Zoê\u0301",), "Zoếcheck", ("target", "ếcheck"), "ếcheck"),
])
def test_selected_name_bounds_the_cut(names, text, before_387, wanted):
    agents = {key: {"name": name} for key, name in zip(("target", "other"), names)}
    assert resolve_agent_by_spoken_name(text, agents) == (before_387[0], wanted), (
        f"before PR 387 returned {before_387!r}")


@pytest.mark.parametrize("name,text,wanted", [
    ("Zoé", "hey Zoécheck now", "écheck now"),
    ("Zoë", "Koko請检查 now", "Koko請检查 now"),
    ("Zoë", "hey Zoé, go", "é, go"),
    ("Zoế", "hey Zoê\u0301check", "ê\u0301check"),
])
def test_judged_cut_uses_the_selected_name(monkeypatch, name, text, wanted):
    from lib import judgment_sites

    monkeypatch.setattr(judgment_sites, "resolve_spoken_name", lambda *a: ("target", text))
    assert resolve_agent_by_spoken_name(text, {"target": {"name": name}}) == ("target", wanted)


@pytest.mark.parametrize("name,text,before_387,wanted", [
    ("Anna Maria", "Anna Mariana please", "Mariana please", "Mariana please"),
    ("Mary Ann", "Mary Annette go", "Annette go", "Annette go"),
    ("Jean Luc", "Jean Lucas go", "Lucas go", "Lucas go"),
    ("Zoë", "Zoëy go", "ëy go", "ëy go"),
    ("Anna Maria Rose", "Anna Maria Rosemary please", "Maria Rosemary please", "Maria Rosemary please"),
    ("Anna Marí", "Anna Maríana please", "Maríana please", "Maríana please"),
    ("Mary Anné", "Mary Annétta go", "Annétta go", "Annétta go"),
    ("Jean Luc Renée", "Jean Luc Renées go", "Luc Renées go", "Luc Renées go"),
    ("Anna Maria", "Anna Maria2 please", "Maria2 please", "Maria2 please"),
    ("Zoë", "Zoë² go", "ë² go", "ë² go"),
    ("Zoë", "Zoë\u0301 go", "ë\u0301 go", "ë\u0301 go"),
    ("Koko", "Koko请检查分支 now", "请检查分支 now", "请检查分支 now"),
    ("Anna Maria", "anna maria please", "maria please", "please"),
    ("Anna Maria", "Anna Maria, please", "Maria, please", "please"),
    ("Anna Maria", "Anna Maria", "Maria", ""),
    ("Anna Maria Rose", "Anna Maria Rose please", "Maria Rose please", "please"),
    ("Jean Luc Renée", "Jean Luc Renée, go", "Luc Renée, go", "go"),
    ("Zoë", "Zoë go", "ë go", "go"),
    ("Rachél", "Rachél, go", "él, go", "go"),
    ("Zoế", "Zoê\u0301, go", "ê\u0301, go", "go"),
    ("Zoé", "Zoécheck", "écheck", "écheck"),
    ("Zoë", "Zoë2 go", "ë2 go", "ë2 go"),
    ("Zoë", "Zoë_Marie go", "ë_Marie go", "_Marie go"),
    ("Zoë", "Zoë-Marie go", "ë-Marie go", "Marie go"),
])
def test_name_extension_requires_a_word_end(name, text, before_387, wanted):
    assert resolve_agent_by_spoken_name(text, {"agent": {"name": name}}) == ("agent", wanted), (
        f"before PR 387 the body was {before_387!r}")
