"""/log under the user's choice (issue 478, piece 4): coordination between agents left out of
the chat before the page limit, the policy echoed on every answer, the place and size of what
was left out counted from the previous VISIBLE row of the chat, and a cache read under another
policy answered with replace_required. No parameter: today's answer.

Each of the iOS owner's requirements is a test of its own name: (a) the echo, (b) the revision,
(c) a setting changed on another device, (d) the counts. (e) the live channel and (f) unread and
push are pinned in test_hidden_coordination.py."""
from __future__ import annotations

import pytest

from lib import agents, clock, conversation, coordination, db, message_writes

BASE = 1_795_000_000_000


class _Chat:
    """One agent's conversation built row by row, in display order."""

    def __init__(self, tmp_path):
        self.id = agents.create_agent(persona="Ivy", session="ivy", voice_id="v", cwd=str(tmp_path),
                                      backend="claude")
        self.boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
        agents.start_runtime(self.id, "ivy")
        agents.bind_backend_session(self.id, "native")
        self.at = 0
        self.seq = 0

    def _row(self, role, text, trace, *, tool=False, origin="user", sender=None):
        self.at += 1_000
        self.seq += 1
        database = db.conn()
        revision = message_writes._next_revision(database)
        mid = f"r{self.seq}"
        database.execute(
            """INSERT INTO messages(message_id, agent_id, backend_session_id, source_file, seq, role, timestamp,
                                    text, tool_name, updated_at, revision, origin, sender_agent_id, trace_id)
               VALUES (?, ?, 'native', 't.jsonl', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, self.id, self.seq, role, clock.iso_from_ms(BASE + self.at), "" if tool else text,
             "Bash" if tool else None, BASE + self.at, revision, origin, sender, trace or None))
        database.execute(
            """INSERT INTO conversation_heads(agent_id, backend_session_id, revision, replace_revision)
               VALUES (?, 'native', ?, 0) ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""", (self.id, revision))
        database.commit()
        return mid

    def user_turn(self, trace):
        return [self._row("user", f"user asks {trace}", trace), self._row("assistant", f"answer {trace}", trace)]

    def coordination_turn(self, trace):
        with db.conn() as con:
            coordination.mark(con, agent_id=self.id, trace_id=trace, sender_agent_id=self.boss)
        db.close_local()
        return [self._row("user", f"boss asks {trace}", trace, origin="agent", sender=self.boss),
                self._row("assistant", "", trace, tool=True, origin="agent", sender=self.boss),
                self._row("assistant", f"reply {trace}", trace, origin="agent", sender=self.boss)]

    def untraced(self):
        return [self._row("assistant", "a row no turn wrote", "")]

    def log(self, mode="hide", **kw):
        return conversation.load_conversation(session="ivy", coordination_mode=mode, **kw)


@pytest.fixture
def chat(tmp_path):
    return _Chat(tmp_path)


def _ids(answer):
    return [turn["id"] for turn in answer["turns"]]


def test_no_parameter_is_todays_answer(chat):
    """R: without coordination= every row comes back, nothing is counted, nothing is echoed."""
    rows = chat.user_turn("u1") + chat.coordination_turn("c1") + chat.user_turn("u2")
    plain = chat.log(mode="")
    assert _ids(plain) == rows
    assert "coordination" not in plain and "hidden_after" not in plain
    assert not any("hidden_before" in turn for turn in plain["turns"])
    assert [turn["audience"] for turn in plain["turns"]] == ["user"] * 2 + ["agents"] * 3 + ["user"] * 2


def test_a_hidden_page_of_n_is_n_rows_the_user_sees(chat):
    for n in range(3):
        chat.user_turn(f"u{n}")
        for k in range(4):
            chat.coordination_turn(f"c{n}-{k}")
    answer = chat.log(limit=4)
    assert len(answer["turns"]) == 4 and answer["has_more"] is True
    assert {turn["audience"] for turn in answer["turns"]} == {"user"}


def test_a_the_echo_names_the_policy_of_every_answer(chat):
    """(a) The newest page, every before-id page and every delta carry the policy they were read
    under; 'auto' names where it came from."""
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    last = chat.user_turn("u2")
    tail = chat.log(mode="auto")
    assert tail["coordination"] == {"hidden": False, "source": "global", "visibility": "show"}
    coordination.set_global("hide")
    tail = chat.log(mode="auto")
    assert tail["coordination"] == {"hidden": True, "source": "global", "visibility": "hide"}
    older = chat.log(mode="auto", before_message_id=last[0], limit=1)
    assert older["coordination"] == tail["coordination"]
    delta = chat.log(mode="auto", after_revision=tail["latest_revision"])
    assert delta["coordination"] == tail["coordination"]
    coordination.set_chat(chat.id, "show")
    assert chat.log(mode="auto")["coordination"] == {"hidden": False, "source": "chat", "visibility": "show"}
    assert chat.log(mode="hide")["coordination"]["source"] == "request"


def test_b_hidden_rows_advance_the_revision_and_a_delta_of_only_them_is_empty(chat):
    """(b)"""
    chat.user_turn("u1")
    tail = chat.log()
    chat.coordination_turn("c1")
    delta = chat.log(after_revision=tail["latest_revision"], coordination_read="hide")
    assert delta["turns"] == [] and delta["latest_revision"] > tail["latest_revision"]
    assert delta["replace_required"] is False
    assert delta["hidden_after"]["messages"] == 2 and delta["hidden_after"]["exchanges"] == 1
    again = chat.log(after_revision=delta["latest_revision"], coordination_read="hide")
    assert again["turns"] == [] and again["latest_revision"] == delta["latest_revision"]


def test_c_a_setting_changed_on_another_device_reloads_once_not_for_ever(chat):
    """(c) A delta that says its cache was read under 'show' gets replace_required once the chat
    is 'hide'; the reload's echo names the new policy, and the deltas after it flow. Nothing was
    written to the conversation for it, so the old cursor alone does not cause it."""
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    cached = chat.log(mode="auto")
    assert cached["coordination"]["visibility"] == "show"
    heads = [tuple(r) for r in db.conn().execute("SELECT * FROM conversation_heads")]
    coordination.set_global("hide")                                       # on another device
    assert [tuple(r) for r in db.conn().execute("SELECT * FROM conversation_heads")] == heads
    delta = chat.log(mode="auto", after_revision=cached["latest_revision"], coordination_read="show")
    assert delta["replace_required"] is True
    reloaded = chat.log(mode="auto")
    assert reloaded["coordination"]["visibility"] == "hide"
    chat.user_turn("u2")
    after = chat.log(mode="auto", after_revision=reloaded["latest_revision"],
                     coordination_read=reloaded["coordination"]["visibility"])
    assert after["replace_required"] is False and len(after["turns"]) == 2


def test_d_a_hidden_run_is_counted_once_on_the_first_visible_row_after_it_whatever_the_page(chat):
    """(d) and L: the same run, read with page sizes that put the page's boundary before, inside
    and after it, gives the same count on the same row."""
    first = chat.user_turn("u1")
    for k in range(3):
        chat.coordination_turn(f"c{k}")
    after_run = chat.user_turn("u2")
    chat.user_turn("u3")
    want = {"messages": 6, "exchanges": 3, "agents": [{"agent_id": chat.boss, "name": "Boss"}],
            "capped": False}
    for limit in (1, 2, 3, 4, 6):
        seen = {}
        answer = chat.log(limit=limit)
        while True:
            seen.update({t["id"]: t.get("hidden_before") for t in answer["turns"]})
            if not answer["has_more"]:
                break
            answer = chat.log(limit=limit, before_message_id=answer["turns"][0]["id"])
        assert seen[after_run[0]] == want, limit
        assert [mid for mid, c in seen.items() if c] == [after_run[0]], limit
        assert seen[first[0]] is None


def test_d_what_follows_the_newest_visible_row_is_counted_after_it(chat):
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    chat.coordination_turn("c2")
    answer = chat.log()
    assert answer["hidden_after"] == {"messages": 4, "exchanges": 2,
                                      "agents": [{"agent_id": chat.boss, "name": "Boss"}],
                                      "capped": False}
    assert not any("hidden_before" in turn for turn in answer["turns"])


def test_m_showing_a_hidden_run_reads_the_chat_with_coordination_show(chat):
    chat.user_turn("u1")
    rows = chat.coordination_turn("c1")
    shown = chat.log(mode="show")
    assert [t["id"] for t in shown["turns"] if t["audience"] == "agents"] == rows
    assert shown["coordination"] == {"hidden": False, "source": "request", "visibility": "show"}


def test_t_a_row_no_turn_wrote_is_shown(chat):
    rows = chat.untraced()
    chat.coordination_turn("c1")
    answer = chat.log()
    assert _ids(answer) == rows and answer["turns"][0]["audience"] == "user"


def test_a_note_in_a_hidden_turn_is_shown_and_the_turns_other_rows_are_not(chat):
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    note = message_writes.record_note(agent_id=chat.id, backend_session_id="native", note_id="n1",
                                      text="For you.", trace_id="c1")
    answer = chat.log()
    assert note["id"] in _ids(answer)
    assert [t["audience"] for t in answer["turns"] if t["id"] == note["id"]] == ["user"]


def test_an_agent_that_cannot_leave_a_note_is_not_filtered(chat):
    with db.conn() as con:
        con.execute("UPDATE agents SET backend='codex' WHERE agent_id=?", (chat.id,))
    db.close_local()
    rows = chat.user_turn("u1") + chat.coordination_turn("c1")
    answer = chat.log()
    assert _ids(answer) == rows and "hidden_after" not in answer
    assert answer["coordination"]["hidden"] is True       # the policy it was read under, as set


def test_o_a_card_published_in_a_coordination_turn_is_in_the_answer_under_hide(chat):
    """O: cards are not chat rows; they are listed for the chat whatever the setting."""
    from lib import artifacts
    chat.coordination_turn("c1")
    coordination.set_global("hide")
    card = artifacts.create(session="ivy", type="document", title="A card", payload={"content": "words"},
                            source_trace_id="c1")
    listed = [a["artifact_id"] for a in artifacts.list_artifacts(session="ivy")]
    assert card["artifact_id"] in listed


# ---- capped counts -----------------------------------------------------------------------------

def test_a_long_run_is_counted_up_to_the_cap_nearest_its_row_and_says_capped(chat, monkeypatch):
    monkeypatch.setattr(coordination, "COUNT_CAP", 4)
    chat.user_turn("u1")
    for k in range(5):
        chat.coordination_turn(f"c{k}")                 # 10 messages, 15 rows
    after = chat.user_turn("u2")
    answer = chat.log()
    count = next(t["hidden_before"] for t in answer["turns"] if t["id"] == after[0])
    assert count["capped"] is True and count["messages"] == 4 and count["exchanges"] == 2
    chat.coordination_turn("c9")
    assert chat.log()["hidden_after"] == {"messages": 2, "exchanges": 1, "capped": False,
                                          "agents": [{"agent_id": chat.boss, "name": "Boss"}]}


def test_the_row_cap_stops_a_run_of_rows_without_words(chat, monkeypatch):
    monkeypatch.setattr(coordination, "COUNT_ROW_CAP", 3)
    chat.user_turn("u1")
    for k in range(3):
        chat.coordination_turn(f"c{k}")                 # 9 rows
    assert chat.log()["hidden_after"]["capped"] is True
    assert chat.log()["hidden_after"]["messages"] <= 3


def test_a_capped_count_is_the_same_whatever_the_page_size(chat, monkeypatch):
    monkeypatch.setattr(coordination, "COUNT_CAP", 3)
    chat.user_turn("u1")
    for k in range(4):
        chat.coordination_turn(f"c{k}")
    after = chat.user_turn("u2")
    chat.user_turn("u3")
    counts = set()
    for limit in (1, 2, 3, 5):
        answer = chat.log(limit=limit)
        while True:
            for t in answer["turns"]:
                if t["id"] == after[0]:
                    counts.add(repr(t["hidden_before"]))
            if not answer["has_more"]:
                break
            answer = chat.log(limit=limit, before_message_id=answer["turns"][0]["id"])
    assert len(counts) == 1 and "'capped': True" in counts.pop()
