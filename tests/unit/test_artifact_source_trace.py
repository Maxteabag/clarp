"""An artifact names the turn that published it (R102), so the apps place its card under that
turn's reply and not by time.

Nothing set ``source_trace_id``. The publishing tools already send the turn's Host-issued token
(X-Clarp-Turn), and each backend records the token where it starts the turn, which knows the
trace; the token is now recorded with its trace. The route sets the trace only when the token's
turn belongs to the artifact's own agent and is that agent's running turn at the publish."""
from __future__ import annotations

import os

import pytest

from lib import agents as agents_db
from lib import artifacts, db, provider_background_jobs as provider_jobs, turn_identity, turn_lifecycle
from lib.routes.artifacts import ArtifactRoutes


@pytest.fixture
def agent(tmp_path):
    return agents_db.create_agent(persona="Ivy", voice_id="v", cwd=str(tmp_path), session="ivy")


def _token(agent_id, trace):
    token = provider_jobs.new_turn_token()
    provider_jobs.turn_started(token, agent_id=agent_id, provider="claude", pid=os.getpid(), trace_id=trace)
    return token


class _Request(ArtifactRoutes):
    def __init__(self, token=""):
        self.headers = {"X-Clarp-Turn": token} if token else {}


def _publish(token, session="ivy", **extra):
    trace = _Request(token)._publishing_trace(session)
    return artifacts.create(session=session, type=extra.pop("type", "document"), title="A card",
                            payload=extra.pop("payload", {"content": "words"}), source_trace_id=trace, **extra)


def test_the_token_is_recorded_with_its_turns_trace(agent):
    token = _token(agent, "trace-a")
    assert turn_identity.caller_turn({provider_jobs.TURN_ENV: token}) == (agent, "trace-a")


def test_a_card_published_in_the_agents_running_turn_names_that_turn(agent):
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    row = _publish(_token(agent, "trace-a"))
    assert row["source_trace_id"] == "trace-a" and row["source_message_id"] == ""


def test_a_card_published_after_its_turn_ended_names_no_turn(agent):
    """A detached job its turn started, publishing later: placed by time, not under a turn of
    hours ago."""
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    token = _token(agent, "trace-a")
    turn_lifecycle.close_turns_for_trace(agent, "trace-a")
    assert _publish(token)["source_trace_id"] == ""


def test_a_card_published_while_a_newer_turn_runs_names_no_turn(agent):
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    token = _token(agent, "trace-a")
    turn_lifecycle.close_turns_for_trace(agent, "trace-a")
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-b")
    assert _publish(token)["source_trace_id"] == ""


def test_another_agents_token_or_none_names_no_turn(agent, tmp_path):
    other = agents_db.create_agent(persona="Oak", voice_id="v", cwd=str(tmp_path), session="oak")
    turn_lifecycle.open_turn(agent_id=other, source="pwa", trace_id="trace-o")
    assert _publish(_token(other, "trace-o"))["source_trace_id"] == ""
    assert _publish("")["source_trace_id"] == ""


def test_a_token_issued_before_the_trace_was_recorded_names_no_turn(agent):
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    assert _publish(_token(agent, ""))["source_trace_id"] == ""


def test_a_later_revision_keeps_the_trace_it_was_published_with(agent):
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    first = _publish(_token(agent, "trace-a"), type="html_form",
                     payload={"content": "<p>a</p>", "version": "1", "read_only": True})
    turn_lifecycle.close_turns_for_trace(agent, "trace-a")
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-b")
    artifacts.update(first["artifact_id"], {"payload_patch": {"content": "<p>b</p>"}})
    assert artifacts.get(first["artifact_id"])["source_trace_id"] == "trace-a"


def test_the_migration_adds_the_column_once():
    from lib import db_migrations
    con = db.conn()
    db_migrations._migrate_to_v129(con)
    columns = [row[1] for row in con.execute("PRAGMA table_info(provider_turns)")]
    assert columns.count("trace_id") == 1


def test_a_render_spec_published_in_the_running_turn_names_it(agent):
    """The kind whose card the user must answer: placed under the wrong reply it is a visible fault."""
    import json
    import pathlib
    form = json.loads((pathlib.Path(__file__).parents[1] / "fixtures/render_spec/build-decision.json").read_text())
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    trace = _Request(_token(agent, "trace-a"))._publishing_trace("ivy")
    row = artifacts.create(session="ivy", type="render_spec", title="Build", payload=form, source_trace_id=trace)
    stored = db.conn().execute("SELECT source_trace_id FROM artifacts WHERE artifact_id=?",
                               (row["artifact_id"],)).fetchone()[0]
    assert stored == "trace-a"


def test_the_trace_is_the_same_string_as_on_the_turns_log_rows_and_live_events(tmp_path, monkeypatch):
    """Both apps place a card by comparing source_trace_id with the trace_id of the turn's rows,
    exactly (R102 (h)). On the real dispatch path: the dispatcher hands spawn_turn the trace it
    gives the turn's user row; a backend records its token with that trace (as every per-turn
    backend does in start_turn); the stream's live row and the imported reply carry it too."""
    import lib.turn_dispatch as td
    from lib import message_store
    from lib.clock import iso_from_ms, now_ms
    from tests.unit.test_queued_recovery_backoff import _Backends, _service
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    tokens = []

    class _Recording(_Backends):
        def spawn_turn(self, backend, **kwargs):
            token = provider_jobs.new_turn_token()                  # what start_turn does
            provider_jobs.turn_started(token, agent_id=kwargs["agent_id"], provider="claude",
                                       pid=os.getpid(), trace_id=kwargs["trace_id"])
            tokens.append((token, kwargs["trace_id"], kwargs["backend_session_id"]))
            message_store.upsert_live_assistant_message(                # the stream's live row
                agent_id=kwargs["agent_id"], backend_session_id=kwargs["backend_session_id"],
                trace_id=kwargs["trace_id"], text="Looking at it.", item_key="m1")
            return super().spawn_turn(backend, **kwargs)
    service, _plain, _clock, _timers = _service(tmp_path)
    service.backends = _Recording()
    agent_id = agents_db.create_agent(persona="Miso", voice_id="V", cwd=str(tmp_path), session="miso",
                                      backend="claude")
    agents_db.start_runtime(agent_id, "miso")
    service.dispatch(text="Make me a form", requested_session="miso", forced_session="miso",
                     trace_id="trace-real-1", client_msg_id="c-real-1", synthesize_audio=False, origin="user")
    [(token, trace, session)] = tokens
    row = artifacts.create(session="miso", type="document", title="Form", payload={"content": "words"},
                           source_trace_id=_Request(token)._publishing_trace("miso"))
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=session, source_file="/t/real.jsonl",
        turns=[{"role": "user", "text": "Make me a form", "timestamp": iso_from_ms(now_ms())},
               {"role": "assistant", "text": "Here it is.", "timestamp": iso_from_ms(now_ms() + 1000)}])
    con = db.conn()
    user = con.execute("SELECT trace_id FROM messages WHERE agent_id=? AND role='user'", (agent_id,)).fetchall()
    live = con.execute("SELECT trace_id FROM messages WHERE agent_id=? AND source_file LIKE 'live:%'",
                       (agent_id,)).fetchall()
    reply = con.execute("SELECT trace_id FROM messages WHERE agent_id=? AND role='assistant' AND text='Here it is.'",
                        (agent_id,)).fetchall()
    assert row["source_trace_id"] == trace == "trace-real-1"
    assert {r[0] for r in user} == {trace} and [r[0] for r in reply] == [trace]
    assert [r[0] for r in live] in ([trace], [])                     # retracted at the import, or still there
    td.reset_for_tests()


def test_a_card_is_dated_when_it_is_published_whichever_way_it_comes(agent, monkeypatch):
    """R102 (i): created_at is the Host's clock at the publish, the one insert every kind goes
    through; not the turn's start, and a later revision leaves it."""
    from lib import db as db_module
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(db_module, "now_ms", lambda: clock["ms"])
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    clock["ms"] += 60_000                                            # the turn began a minute ago
    document = artifacts.create(session="ivy", type="document", title="D", payload={"content": "w"})
    report = artifacts.create(session="ivy", type="html_form", title="R",
                              payload={"content": "<p>a</p>", "version": "1", "read_only": True})
    assert document["created_at"] == report["created_at"] == clock["ms"]
    clock["ms"] += 5_000
    artifacts.update(report["artifact_id"], {"payload_patch": {"content": "<p>b</p>"}})
    assert artifacts.get(report["artifact_id"])["created_at"] == clock["ms"] - 5_000


# The previous release's writes to provider_turns, word for word (main before R102): after a rollback
# with --allow-older-schema it runs against a schema 129 database.
_PREVIOUS_INSERT = """INSERT OR IGNORE INTO provider_turns
        (turn_token,agent_id,provider,pid,start_token,started_at) VALUES (?,?,?,?,?,?)"""
_PREVIOUS_EXIT = """UPDATE provider_turns SET exited_at=?
            WHERE turn_token=? AND exited_at IS NULL"""


def test_the_previous_releases_writes_still_work_against_schema_129(agent):
    """Rolled back, the older code knows no trace_id: its insert leaves the column's default, its
    turn names no trace, and a card it publishes is placed by time, as before."""
    from lib import db_schema
    con = db.conn()
    assert db_schema._SCHEMA_VERSION >= 129
    token = provider_jobs.new_turn_token()
    con.execute(_PREVIOUS_INSERT, (token, agent, "claude", os.getpid(), "start", 1))
    assert con.execute("SELECT trace_id FROM provider_turns WHERE turn_token=?", (token,)).fetchone()[0] == ""
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="trace-a")
    assert _publish(token)["source_trace_id"] == ""
    assert con.execute(_PREVIOUS_EXIT, (2, token)).rowcount == 1
