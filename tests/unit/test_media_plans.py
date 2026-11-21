"""The gallery's reads (issue 489) are served by their indexes: no walk of media_assets, no
sort of a chat. The SQL is captured from the store as it runs, then explained."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import db, media_store  # noqa: E402

IMAGES = "('image/gif','image/heic','image/heif','image/jpeg','image/png','image/webp')"


@pytest.fixture
def con(tmp_path):
    db.reset_for_tests(tmp_path / "plans.sqlite")
    c = db.conn()
    c.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at) "
              "VALUES ('ag', 'Mike', 'v', '/tmp', 'chat', 1)")
    rows = [(f"asset_{i:05d}", "ag", "chat" if i % 3 else "other", "a.png", f"s{i}",
             "image/png" if i % 7 else "text/plain", 1, f"/b/{i}", "agent", 1000 + i // 2,
             2000 + i if i % 11 == 0 else None, 3000 + i if i % 5 == 0 else None)
            for i in range(600)]
    c.executemany("INSERT INTO media_assets(asset_id, agent_id, session, source_name, sha256, "
                  "mime_type, bytes, storage_path, created_by, created_at, deleted_at, favorite_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.execute("ANALYZE")
    return c


def _plans(con, call) -> list[tuple[str, list[str]]]:
    seen: list[str] = []
    con.set_trace_callback(seen.append)
    try:
        call()
    finally:
        con.set_trace_callback(None)
    selects = [sql for sql in seen if sql.lstrip().upper().startswith("SELECT")]
    return [(sql, _plan(con, sql)) for sql in selects]


def _plan(con, sql: str) -> list[str]:
    return [row[3] for row in con.execute("EXPLAIN QUERY PLAN " + sql)]


def _media_steps(plans):
    return [(sql, step) for sql, steps in plans for step in steps
            if "media_assets" in step or "TEMP B-TREE" in step]


def _assert_indexed(plans, *indexes):
    steps = _media_steps(plans)
    assert steps
    for sql, step in steps:
        assert "TEMP B-TREE" not in step, (sql, step)
        assert not step.startswith("SCAN media_assets"), (sql, step)
        assert any(index in step for index in indexes), (sql, step)


def test_a_gallery_page_and_its_totals_walk_only_their_indexes(con):
    page = media_store.list_page("chat", limit=10)
    plans = _plans(con, lambda: media_store.list_page("chat", limit=10))
    _assert_indexed(plans, "idx_media_assets_session_page", "idx_media_assets_session_favorite")
    plans = _plans(con, lambda: media_store.list_page("chat", limit=10, before=page["next_before"]))
    _assert_indexed(plans, "idx_media_assets_session_page", "idx_media_assets_session_favorite")
    # The page seeks its cursor, not the top of the chat.
    [seek] = [steps for sql, steps in plans if "ORDER BY created_at" in sql]
    assert seek == ["SEARCH media_assets USING INDEX idx_media_assets_session_page "
                    "(session=? AND (created_at,asset_id)<(?,?))"], seek
    # The chat's image count reads the page index alone.
    counts = {("mime_type" in sql): steps for sql, steps in plans if "COUNT(*)" in sql}
    assert counts[True] == [
        "SEARCH media_assets USING COVERING INDEX idx_media_assets_session_page (session=?)"]
    assert counts[False] == ["SEARCH media_assets USING COVERING INDEX "
                             "idx_media_assets_session_favorite (session=? AND favorite_at>?)"]


def test_a_favourites_page_walks_the_favourites_index(con):
    page = media_store.list_page("chat", limit=3, favorites=True)
    for before in ("", page["next_before"]):
        plans = _plans(con, lambda: media_store.list_page(
            "chat", limit=3, favorites=True, before=before))
        _assert_indexed(plans, "idx_media_assets_session_favorite", "idx_media_assets_session_page")
        [seek] = [steps for sql, steps in plans if "ORDER BY favorite_at" in sql]
        assert seek == ["SEARCH media_assets USING INDEX idx_media_assets_session_favorite "
                        "(session=? AND favorite_at>?" + (" AND (favorite_at,asset_id)<(?,?))"
                                                          if before else ")")], seek


def test_the_deleted_feed_reads_a_tombstone_index_alone(con):
    for session, index in (("", "idx_media_assets_deleted"), ("chat", "idx_media_assets_session_deleted")):
        plans = _plans(con, lambda: media_store.deleted_since(since=0, session=session, limit=5))
        assert plans, session
        for sql, steps in plans:
            assert steps and all(f"USING COVERING INDEX {index} " in s for s in steps), (sql, steps)


def test_used_as_is_one_query_per_page_by_agent(con):
    con.execute("INSERT INTO agent_portraits(portrait_id, agent_id, media_asset_id, storage_path, "
                "sha256, mime_type, created_by, created_at, is_primary) "
                "VALUES ('p1', 'ag', 'asset_00001', '/b/1', 's1', 'image/png', 'agent', 1, 1)")
    plans = _plans(con, lambda: media_store.list_page("chat", limit=50))
    portraits = [(sql, steps) for sql, steps in plans if "agent_portraits" in sql]
    assert len(portraits) == 1
    sql, steps = portraits[0]
    assert any("idx_agent_portraits_agent_created" in s for s in steps), steps
    assert not any(s.startswith("SCAN") for s in steps), steps


def test_releasing_a_file_finds_its_assets_by_content_address(con):
    plans = _plans(con, lambda: media_store.release_blob("/b/1", "s1"))
    steps = [s for _, ss in plans for s in ss if "media_assets" in s]
    assert steps and all("idx_media_assets_sha" in s for s in steps), steps


def test_the_list_query_of_code_from_before_schema_131_is_served_by_the_page_index(con):
    """After a rollback, older code runs main's list query; the page index replaces the
    created index it used, with the same leading columns."""
    old = (f"SELECT * FROM media_assets WHERE session = 'chat' AND deleted_at IS NULL "
           f"AND mime_type IN {IMAGES} ORDER BY created_at DESC LIMIT 100")
    steps = _plan(con, old)
    assert steps == ["SEARCH media_assets USING INDEX idx_media_assets_session_page (session=?)"], steps
