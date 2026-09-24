from lib import agents as agents_db
from lib import focus


def test_no_focus_is_empty_string():
    assert focus.current_focus_session() == ""


def test_reads_the_db_focus():
    agent_id = agents_db.create_agent(persona="Rachel", voice_id="V", cwd="/tmp",
                                      session="rachel")
    agents_db.set_focus(agent_id)
    assert focus.current_focus_session() == "rachel"
    agents_db.set_focus(None)
    assert focus.current_focus_session() == ""


def test_lookup_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(agents_db, "get_focus_session", boom)
    assert focus.current_focus_session() == ""
