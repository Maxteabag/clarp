"""Delete and favourite a chat's gallery images (feature media_manage, issue 489), through
the real routes of a running Host: the three writes, the 410, paging and favourites, the
row keys, used_as / still_used_as, the media-changed event, who may, and the bytes."""
from __future__ import annotations

import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib import agent_portraits, db, device_pairing, media_store, server_identity  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
build_server = server_module.build_server

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)
TOKEN = "media-manage-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = "lena-7b4b"


def _png(n: int) -> bytes:
    """A distinct picture per n (bytes after IEND change the content address only)."""
    return PNG + f"#{n}".encode()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Host:
    def __init__(self, base, media_dir, events):
        self.base, self.media_dir, self.events = base, media_dir, events

    def call(self, method, path, body=None, *, headers=None, raw=None, ctype=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        hdrs = {**AUTH, **(headers or {})}
        if data is not None:
            hdrs.setdefault("Content-Type", ctype or "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload

    def publish(self, blob, *, ctype="image/png", name="pic.png"):
        status, out = self.call("POST", "/media", raw=blob, ctype=ctype, headers={
            "X-Session": SESSION, "X-File-Name": urllib.parse.quote(name)})
        assert status == 200, out
        return out["asset"]

    def media_events(self):
        return [e for e in self.events if e.get("type") == "media-changed"]


@pytest.fixture
def host(tmp_path):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"
    audio.mkdir()
    from lib.agents import create_agent
    create_agent(persona="Lena", voice_id="V_LENA", cwd=str(tmp_path), session=SESSION)
    stream = AudioStream(audio)
    seen: list[dict] = []
    original = stream.broadcast

    def recording(event):
        seen.append(dict(event))
        return original(event)

    stream.broadcast = recording
    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio, agents_path=tmp_path / "agents.json",
        default_session=SESSION, uploads_dir=tmp_path / "uploads", media_dir=tmp_path / "media",
        tts=FakeTTSEngine(audio), stream=stream, stt=StubSTT(text="hi", ends_terminal=True),
        roster_names=("Lena",), auth_token=TOKEN,
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/agents/snapshot", headers=AUTH), timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield Host(base, tmp_path / "media", seen)
    srv.shutdown()
    srv.server_close()


def _blob(asset) -> pathlib.Path:
    return pathlib.Path(media_store.lookup(asset["asset_id"])["storage_path"])


# ---- the row, the feature, publish ------------------------------------------------

def test_feature_is_advertised_and_publish_answers_the_new_keys_and_event(host):
    assert "media_manage" in server_identity.FEATURES
    asset = host.publish(_png(1))
    assert asset["favorite"] is False and asset["favorite_at"] is None and asset["used_as"] == []
    [event] = host.media_events()
    assert event["change"] == "created" and event["asset_id"] == asset["asset_id"]
    assert event["session"] == SESSION and event["at"] == asset["created_at"]
    assert "favorite" not in event and event["agent_id"]
    # A file that is not an image lands in no gallery and sends no event.
    host.publish(b"plain words", ctype="text/plain", name="notes.txt")
    assert len(host.media_events()) == 1


# ---- favourite -------------------------------------------------------------------

def test_favourite_sets_once_keeps_its_time_and_unsets(host):
    asset = host.publish(_png(1))
    aid = asset["asset_id"]
    status, row = host.call("POST", f"/media/{aid}/favorite", {"favorite": True})
    assert status == 200 and row["favorite"] is True and isinstance(row["favorite_at"], int)
    first_at = row["favorite_at"]
    time.sleep(0.005)
    status, again = host.call("POST", f"/media/{aid}/favorite", {"favorite": True})
    assert status == 200 and again == row and again["favorite_at"] == first_at
    favourite_events = [e for e in host.media_events() if e["change"] == "favorite"]
    assert [(e["favorite"], e["at"]) for e in favourite_events] == [(True, first_at)]
    status, off = host.call("POST", f"/media/{aid}/favorite", {"favorite": False})
    assert status == 200 and off["favorite"] is False and off["favorite_at"] is None
    assert [e["favorite"] for e in host.media_events() if e["change"] == "favorite"] == [True, False]
    status, listed = host.call("GET", f"/media?session={SESSION}")
    assert listed["assets"][0]["favorite"] is False and listed["favorites_total"] == 0


def test_favourite_refusals(host):
    asset = host.publish(_png(1))
    aid = asset["asset_id"]
    assert host.call("POST", f"/media/{aid}/favorite", {"favorite": "yes"})[0] == 400
    assert host.call("POST", f"/media/{aid}/favorite", {})[0] == 400
    assert host.call("POST", "/media/asset_nope/favorite", {"favorite": True})[0] == 404
    doc = host.publish(b"plain words", ctype="text/plain", name="notes.txt")
    assert host.call("POST", f"/media/{doc['asset_id']}/favorite", {"favorite": True}) == (
        422, {"error": "not_an_image"})
    status, deleted = host.call("DELETE", f"/media/{aid}")
    assert status == 200
    assert host.call("POST", f"/media/{aid}/favorite", {"favorite": True}) == (
        410, {"error": "removed", "asset_id": aid, "deleted_at": deleted["deleted_at"]})


# ---- delete, the 410 and the bytes ---------------------------------------------------

def test_delete_the_only_user_removes_the_file_and_answers_410_for_ever(host):
    asset = host.publish(_png(1))
    aid, blob = asset["asset_id"], _blob(asset)
    assert blob.is_file()
    status, out = host.call("DELETE", f"/media/{aid}")
    assert status == 200
    assert out == {"asset_id": aid, "deleted": True, "deleted_at": out["deleted_at"],
                   "still_used_as": []}
    assert not blob.exists()
    gone = (410, {"error": "removed", "asset_id": aid, "deleted_at": out["deleted_at"]})
    assert host.call("GET", f"/media/{aid}") == gone
    assert host.call("GET", "/media/asset_never_was")[0] == 404
    status, listed = host.call("GET", f"/media?session={SESSION}")
    assert listed["assets"] == [] and listed["total"] == 0
    [event] = [e for e in host.media_events() if e["change"] == "deleted"]
    assert event == {"type": "media-changed", "session": SESSION, "agent_id": event["agent_id"],
                     "asset_id": aid, "change": "deleted", "at": out["deleted_at"],
                     **{k: event[k] for k in ("event_id", "ts") if k in event}}
    row = media_store.lookup(aid)
    assert row["deleted_by"] == "administrator"


def test_a_repeated_delete_answers_the_first_time_and_sends_one_event(host):
    aid = host.publish(_png(1))["asset_id"]
    first = host.call("DELETE", f"/media/{aid}")
    time.sleep(0.005)
    assert host.call("DELETE", f"/media/{aid}") == first
    assert len([e for e in host.media_events() if e["change"] == "deleted"]) == 1
    assert host.call("DELETE", "/media/asset_nope") == (404, {"error": "not_found"})
    doc = host.publish(b"plain words", ctype="text/plain", name="notes.txt")
    assert host.call("DELETE", f"/media/{doc['asset_id']}") == (422, {"error": "not_an_image"})


def test_same_bytes_twice_the_file_stays_and_goes_with_the_last(host):
    first, second = host.publish(_png(1)), host.publish(_png(1))
    blob = _blob(first)
    assert blob == _blob(second)
    assert host.call("DELETE", f"/media/{first['asset_id']}")[0] == 200
    assert blob.is_file()
    status, content = host.call("GET", f"/media/{second['asset_id']}")
    assert status == 200 and content == _png(1)
    assert host.call("DELETE", f"/media/{second['asset_id']}")[0] == 200
    assert not blob.exists()


def test_a_delete_while_the_file_is_already_missing_still_answers_200(host):
    asset = host.publish(_png(1))
    _blob(asset).unlink()
    status, out = host.call("DELETE", f"/media/{asset['asset_id']}")
    assert status == 200 and out["deleted"] is True


def test_two_deletes_of_one_id_at_once(host):
    asset = host.publish(_png(1))
    blob, results = _blob(asset), []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(host.call("DELETE", f"/media/{asset['asset_id']}"))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert [r[0] for r in results] == [200, 200]
    assert results[0][1] == results[1][1]
    assert not blob.exists()
    assert len([e for e in host.media_events() if e["change"] == "deleted"]) == 1


def test_a_crash_between_the_row_and_the_file_is_mended_by_the_next_delete(host, monkeypatch):
    """The row is marked first, the file removed after: a crash between them leaves a
    deleted row and its file (never a live row without its file); deleting again removes it."""
    asset = host.publish(_png(1))
    aid, blob = asset["asset_id"], _blob(asset)

    def crash(*_args):
        raise RuntimeError("power cut")

    monkeypatch.setattr(media_store, "release_blob", crash)
    import http.client
    try:  # the request dies with the "crash": no answer, or a 500
        assert host.call("DELETE", f"/media/{aid}")[0] == 500
    except (http.client.RemoteDisconnected, ConnectionError):
        pass
    assert media_store.lookup(aid)["deleted_at"] is not None and blob.is_file()
    assert host.call("GET", f"/media/{aid}")[0] == 410
    monkeypatch.undo()
    status, out = host.call("DELETE", f"/media/{aid}")
    assert status == 200 and out["deleted_at"] == media_store.lookup(aid)["deleted_at"]
    assert not blob.exists()


# ---- a portrait made from an image ----------------------------------------------------

def _portrait(host, aid):
    status, out = host.call("POST", "/agent-portraits",
                            {"action": "add_media_asset", "session": SESSION, "asset_id": aid})
    assert status == 200, out
    return next(p for p in out["portraits"] if p["media_asset_id"] == aid)


def test_a_portrait_made_from_the_image_keeps_showing_and_is_named(host):
    asset = host.publish(_png(1))
    aid, blob = asset["asset_id"], _blob(asset)
    portrait = _portrait(host, aid)
    agent_id = media_store.lookup(aid)["agent_id"]
    used = [{"kind": "portrait", "agent_id": agent_id, "persona": "Lena"}]
    status, listed = host.call("GET", f"/media?session={SESSION}")
    assert listed["assets"][0]["used_as"] == used
    status, out = host.call("DELETE", f"/media/{aid}")
    assert status == 200 and out["still_used_as"] == used
    # The portrait holds the very same file: it stays for it.
    assert blob.is_file()
    status, content = host.call("GET", portrait["url"])
    assert status == 200 and content == _png(1)
    assert host.call("GET", f"/media/{aid}")[0] == 410
    # When the portrait is retired, the file goes with it (the last user).
    others = [host.publish(_png(n)) for n in (2, 3)]
    agent_portraits.replace_alternates_with_media_assets(
        session=SESSION, asset_ids=[a["asset_id"] for a in others], portrait_dir=host.media_dir)
    assert not blob.exists()
    assert all(_blob(a).is_file() for a in others)


def test_a_selected_portrait_is_also_the_avatar_and_keeps_the_file(host):
    asset = host.publish(_png(1))
    aid, blob = asset["asset_id"], _blob(asset)
    portrait = _portrait(host, aid)
    status, _ = host.call("POST", "/agent-portraits", {
        "action": "select_primary", "session": SESSION, "portrait_id": portrait["portrait_id"]})
    assert status == 200
    assert host.call("DELETE", f"/media/{aid}")[0] == 200
    assert blob.is_file()
    assert host.call("GET", portrait["url"])[0] == 200


# ---- several at once ---------------------------------------------------------------

def test_batch_delete_answers_per_id_in_order(host):
    a, b = host.publish(_png(1)), host.publish(_png(2))
    doc = host.publish(b"plain words", ctype="text/plain", name="notes.txt")
    status, out = host.call("POST", "/media/delete", {
        "asset_ids": [b["asset_id"], "asset_nope", doc["asset_id"], a["asset_id"], b["asset_id"]]})
    assert status == 200
    results = out["results"]
    assert [r["asset_id"] for r in results] == [
        b["asset_id"], "asset_nope", doc["asset_id"], a["asset_id"], b["asset_id"]]
    assert results[0]["deleted"] is True and results[0]["still_used_as"] == []
    assert results[1] == {"asset_id": "asset_nope", "deleted": False, "error": "not_found"}
    assert results[2] == {"asset_id": doc["asset_id"], "deleted": False, "error": "not_an_image"}
    assert results[3]["deleted"] is True and results[4] == results[0]
    assert sorted(e["asset_id"] for e in host.media_events() if e["change"] == "deleted") == sorted(
        [a["asset_id"], b["asset_id"]])
    assert not _blob(a).exists() and not _blob(b).exists()
    for bad in ({}, {"asset_ids": []}, {"asset_ids": ["x"] * 201}, {"asset_ids": [1]},
                {"asset_ids": "x"}):
        assert host.call("POST", "/media/delete", bad)[0] == 400


# ---- who may ----------------------------------------------------------------------

def test_a_limited_device_reads_but_may_not_write(host):
    aid = host.publish(_png(1))["asset_id"]
    limited = device_pairing.exchange(device_pairing.issue(scope="limited")["code"],
                                      device_name="Limited")["token"]
    as_limited = {"Authorization": f"Bearer {limited}"}
    refused = (403, {"error": "full device access required"})
    assert host.call("POST", f"/media/{aid}/favorite", {"favorite": True}, headers=as_limited) == refused
    assert host.call("DELETE", f"/media/{aid}", headers=as_limited) == refused
    assert host.call("POST", "/media/delete", {"asset_ids": [aid]}, headers=as_limited) == refused
    status, listed = host.call("GET", f"/media?session={SESSION}", headers=as_limited)
    assert status == 200 and listed["assets"][0]["favorite"] is False
    assert media_store.lookup(aid)["deleted_at"] is None


def test_a_full_device_may_and_is_recorded(host):
    aid = host.publish(_png(1))["asset_id"]
    paired = device_pairing.exchange(device_pairing.issue(scope="full")["code"],
                                     device_name="Phone")
    status, _ = host.call("DELETE", f"/media/{aid}",
                          headers={"Authorization": f"Bearer {paired['token']}"})
    assert status == 200
    assert media_store.lookup(aid)["deleted_by"].startswith("device_")


def test_a_request_carrying_a_turn_token_is_refused(host):
    aid = host.publish(_png(1))["asset_id"]
    turn = {"X-Clarp-Turn": "any-turn-token"}
    refused = (403, {"error": "agents_may_not"})
    assert host.call("POST", f"/media/{aid}/favorite", {"favorite": True}, headers=turn) == refused
    assert host.call("DELETE", f"/media/{aid}", headers=turn) == refused
    assert host.call("POST", "/media/delete", {"asset_ids": [aid]}, headers=turn) == refused
    assert media_store.lookup(aid)["deleted_at"] is None
    assert media_store.lookup(aid)["favorite_at"] is None


# ---- the list --------------------------------------------------------------------

def _stamp(column, times):
    for aid, at in times.items():
        db.conn().execute(f"UPDATE media_assets SET {column} = ? WHERE asset_id = ?", (at, aid))


def test_pages_walk_the_chat_newest_first_without_repeats(host):
    ids = [host.publish(_png(n))["asset_id"] for n in range(7)]
    # Ties on created_at break by asset_id.
    _stamp("created_at", dict(zip(ids, [1000, 1000, 1000, 2000, 3000, 3000, 4000])))
    host.publish(b"plain words", ctype="text/plain", name="notes.txt")
    expected = [aid for _, aid in sorted(
        ((media_store.lookup(aid)["created_at"], aid) for aid in ids), reverse=True)]
    status, plain = host.call("GET", f"/media?session={SESSION}")
    assert status == 200 and [a["asset_id"] for a in plain["assets"]] == expected
    assert plain["next_before"] is None and plain["total"] == 7 and plain["favorites_total"] == 0
    seen, before = [], ""
    while True:
        query = f"/media?session={SESSION}&limit=3" + (
            f"&before={urllib.parse.quote(before)}" if before else "")
        status, page = host.call("GET", query)
        assert status == 200 and page["total"] == 7
        seen += [a["asset_id"] for a in page["assets"]]
        if page["next_before"] is None:
            break
        before = page["next_before"]
    assert seen == expected
    assert host.call("GET", f"/media?session={SESSION}&before=nonsense") == (
        400, {"error": "bad before"})


def test_favourites_have_their_own_order_paging_and_total(host):
    ids = [host.publish(_png(n))["asset_id"] for n in range(5)]
    marked = [ids[3], ids[0], ids[4], ids[1]]
    for aid in marked:
        assert host.call("POST", f"/media/{aid}/favorite", {"favorite": True})[0] == 200
    _stamp("favorite_at", dict(zip(marked, [5000, 6000, 6000, 7000])))
    expected = [aid for _, aid in sorted(
        ((media_store.lookup(aid)["favorite_at"], aid) for aid in marked), reverse=True)]
    seen, before = [], ""
    while True:
        query = f"/media?session={SESSION}&favorites=1&limit=1" + (
            f"&before={urllib.parse.quote(before)}" if before else "")
        status, page = host.call("GET", query)
        assert status == 200 and page["favorites_total"] == 4 and page["total"] == 5
        assert all(a["favorite"] for a in page["assets"])
        seen += [a["asset_id"] for a in page["assets"]]
        if page["next_before"] is None:
            break
        before = page["next_before"]
    assert seen == expected
    # A cursor of one order is not a cursor of the other.
    status, first = host.call("GET", f"/media?session={SESSION}&limit=1")
    assert host.call("GET", f"/media?session={SESSION}&favorites=1&before="
                     + urllib.parse.quote(first["next_before"]))[0] == 400
    # A deleted favourite leaves the list and the count.
    assert host.call("DELETE", f"/media/{expected[0]}")[0] == 200
    status, page = host.call("GET", f"/media?session={SESSION}&favorites=1")
    assert [a["asset_id"] for a in page["assets"]] == expected[1:]
    assert page["favorites_total"] == 3 and page["total"] == 4


# ---- a device that was away (section 3b) -----------------------------------------

def _deleted(host, query, headers=None):
    status, out = host.call("GET", "/media/deleted?" + query, headers=headers)
    assert status == 200, out
    return out


def test_deleted_since_lists_oldest_first_and_is_matched_before_the_content_route(host):
    ids = [host.publish(_png(n))["asset_id"] for n in range(3)]
    for aid in ids:
        assert host.call("DELETE", f"/media/{aid}")[0] == 200
    out = _deleted(host, "since=0")
    stamps = {aid: media_store.lookup(aid)["deleted_at"] for aid in ids}
    expected = sorted(ids, key=lambda aid: (stamps[aid], aid))
    assert [d["asset_id"] for d in out["deleted"]] == expected
    assert all(d == {"asset_id": d["asset_id"], "session": SESSION,
                     "deleted_at": stamps[d["asset_id"]]} for d in out["deleted"])
    assert out["next_since"] is None and out["server_now"] >= max(stamps.values())
    # Asking again with server_now: a delete made after the answer always comes.
    later = host.publish(_png(9))["asset_id"]
    assert host.call("DELETE", f"/media/{later}")[0] == 200
    assert later in [d["asset_id"] for d in _deleted(host, f"since={out['server_now']}")["deleted"]]
    assert host.call("GET", "/media/deleted")[0] == 400
    assert host.call("GET", "/media/deleted?since=-1")[0] == 400
    assert host.call("GET", "/media/deleted?since=abc")[0] == 400
    for limit in ("x", "0", "-1", "1001"):
        assert host.call("GET", f"/media/deleted?since=0&limit={limit}") == (
            400, {"error": "limit must be a number from 1 to 1000"}), limit
    for limit in ("1", "1000"):
        assert host.call("GET", f"/media/deleted?since=0&limit={limit}")[0] == 200, limit


def _tombstones(n, *, at, prefix="asset_t", session=SESSION):
    """n images deleted at the given ms (a function of the index), written straight to the
    table: the paging rule is about rows, not about how fast a Host can delete."""
    agent_id = db.conn().execute("SELECT agent_id FROM agents WHERE session=?",
                                 (session,)).fetchone()[0]
    rows = [(f"{prefix}{i:05d}", agent_id, session, "t.png", f"t{i}", "image/png", 1,
             f"/nowhere/{prefix}{i}", "agent", 1, at(i)) for i in range(n)]
    db.conn().executemany(
        "INSERT INTO media_assets(asset_id, agent_id, session, source_name, sha256, mime_type, "
        "bytes, storage_path, created_by, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    return [r[0] for r in rows]


def _walk(host, query, since=0):
    pages, seen = [], []
    while since is not None:
        out = _deleted(host, f"since={since}&{query}")
        got = [(d["deleted_at"], d["asset_id"]) for d in out["deleted"]]
        assert all(at > since for at, _ in got)  # strictly after since: never a repeat
        pages.append(got)
        seen += got
        if out["next_since"] is not None:
            assert out["next_since"] == got[-1][0] > since  # always advances
        since = out["next_since"]
        assert len(pages) < 50
    return pages, seen


def test_two_deleted_in_one_millisecond_across_a_page_boundary_both_arrive(host):
    ids = _tombstones(5, at=lambda i: [100, 200, 200, 300, 400][i])
    pages, seen = _walk(host, "limit=2")
    assert seen == sorted(zip([100, 200, 200, 300, 400], ids))
    # The limit falls inside ms 200: the first page takes all of it, past the limit.
    assert [[at for at, _ in page] for page in pages] == [[100, 200, 200], [300, 400]]


def test_three_thousand_in_one_millisecond_come_in_one_page(host):
    ids = _tombstones(3000, at=lambda i: 500)
    _tombstones(2, at=lambda i: 600 + i, prefix="asset_u")
    out = _deleted(host, "since=0&limit=1000")
    assert len(out["deleted"]) == 3000 and {d["asset_id"] for d in out["deleted"]} == set(ids)
    assert out["next_since"] == 500
    rest = _deleted(host, "since=500&limit=1000")
    assert [d["deleted_at"] for d in rest["deleted"]] == [600, 601] and rest["next_since"] is None


def test_asking_with_next_since_never_repeats_and_always_advances(host):
    stamps = [1000 + (i // 3) for i in range(40)]  # three per millisecond
    ids = _tombstones(40, at=lambda i: stamps[i])
    for limit in (1, 2, 3, 4, 7, 39, 40, 1000):
        _, seen = _walk(host, f"limit={limit}")
        assert seen == sorted(zip(stamps, ids)), limit
        assert len(seen) == len(set(seen)), limit


def test_a_since_after_everything_answers_empty(host):
    _tombstones(3, at=lambda i: 700 + i)
    out = _deleted(host, "since=702")
    assert out["deleted"] == [] and out["next_since"] is None
    out = _deleted(host, f"since={10 ** 15}")
    assert out["deleted"] == [] and out["next_since"] is None


def test_deleted_since_filters_by_chat_and_a_limited_device_may_read_it(host):
    from lib.agents import create_agent
    create_agent(persona="Otto", voice_id="V_OTTO", cwd="/tmp", session="otto-1")
    mine = host.publish(_png(1))["asset_id"]
    status, other = host.call("POST", "/media", raw=_png(2), ctype="image/png",
                              headers={"X-Session": "otto-1", "X-File-Name": "o.png"})
    assert status == 200
    for aid in (mine, other["asset_id"]):
        assert host.call("DELETE", f"/media/{aid}")[0] == 200
    assert [d["asset_id"] for d in _deleted(host, f"since=0&session={SESSION}")["deleted"]] == [mine]
    assert {d["asset_id"] for d in _deleted(host, "since=0")["deleted"]} == {mine, other["asset_id"]}
    limited = device_pairing.exchange(device_pairing.issue(scope="limited")["code"],
                                      device_name="Limited")["token"]
    out = _deleted(host, "since=0", headers={"Authorization": f"Bearer {limited}"})
    assert {d["asset_id"] for d in out["deleted"]} == {mine, other["asset_id"]}


# ---- an image card ----------------------------------------------------------------

def test_an_artifact_pointing_at_a_deleted_image_answers_410_on_its_media_url(host):
    asset = host.publish(_png(1))
    status, created = host.call("POST", "/artifacts", {
        "session": SESSION, "type": "file", "title": "A picture",
        "payload": {"url": asset["url"], "mime_type": "image/png", "file_name": asset["file_name"]}})
    assert status == 201, created
    artifact_id = created["artifact"]["artifact_id"]
    assert host.call("DELETE", f"/media/{asset['asset_id']}")[0] == 200
    status, card = host.call("GET", f"/artifacts/{artifact_id}")
    assert status == 200
    card = card.get("artifact", card)
    url = card["payload"]["url"]
    assert url == f"/media/{asset['asset_id']}"
    assert host.call("GET", url) == (410, {"error": "removed", "asset_id": asset["asset_id"],
                                          "deleted_at": media_store.lookup(asset["asset_id"])["deleted_at"]})


# ---- a delete between a portrait's read and its write lock ------------------------

class _DeleteBeforeTheLock:
    """db.conn() wrapped so that, after the portrait code has read the asset as live, the
    image is deleted (for real, through the store, committing) just before the portrait's
    BEGIN IMMEDIATE: the window between the check and the write lock."""

    def __init__(self, monkeypatch, asset_id):
        self.asset_id, self.armed, self.fired = asset_id, False, False
        real = db.conn
        state = self

        class Hooked:
            def __init__(self, con):
                self._con = con

            def execute(self, sql, *args):
                text = " ".join(str(sql).split())
                if text.startswith("SELECT * FROM media_assets") and "deleted_at IS NULL" in text:
                    state.armed = True
                elif text == "BEGIN IMMEDIATE" and state.armed and not state.fired:
                    state.fired = True
                    media_store.delete(state.asset_id, deleted_by="race")
                return self._con.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._con, name)

        monkeypatch.setattr(db, "conn", lambda: Hooked(real()))


def test_a_portrait_is_not_made_from_an_image_deleted_before_its_lock(host, monkeypatch):
    asset = host.publish(_png(1))
    aid, blob = asset["asset_id"], _blob(asset)
    hook = _DeleteBeforeTheLock(monkeypatch, aid)
    status, out = host.call("POST", "/agent-portraits",
                            {"action": "add_media_asset", "session": SESSION, "asset_id": aid})
    monkeypatch.undo()
    assert hook.fired
    assert status == 404 and out == {"error": "media asset not found"}
    assert not blob.exists()
    assert db.conn().execute("SELECT COUNT(*) FROM agent_portraits WHERE media_asset_id=?",
                             (aid,)).fetchone()[0] == 0
    assert host.call("GET", f"/media/{aid}")[0] == 410


def test_generated_alternates_are_not_made_from_an_image_deleted_before_their_lock(host, monkeypatch):
    first, second = host.publish(_png(1)), host.publish(_png(2))
    hook = _DeleteBeforeTheLock(monkeypatch, first["asset_id"])
    with pytest.raises(agent_portraits.PortraitError) as refused:
        agent_portraits.replace_alternates_with_media_assets(
            session=SESSION, asset_ids=[first["asset_id"], second["asset_id"]],
            portrait_dir=host.media_dir)
    monkeypatch.undo()
    assert hook.fired and refused.value.status == 404
    assert not _blob(first).exists() and _blob(second).is_file()
    assert db.conn().execute(
        "SELECT COUNT(*) FROM agent_portraits WHERE media_asset_id IN (?, ?)",
        (first["asset_id"], second["asset_id"])).fetchone()[0] == 0


def _add_portrait(aid, media_dir, results):
    def action():
        try:
            results.append(agent_portraits.add_media_asset(
                session=SESSION, asset_id=aid, portrait_dir=media_dir))
        except agent_portraits.PortraitError as error:
            results.append(error)
    return action


def _before(monkeypatch, name, action):
    """Run `action` once just before media_store.<name> (the delete's mark, or its release
    of the file under the write lock)."""
    original = getattr(media_store, name)

    def hooked(*args, **kwargs):
        monkeypatch.setattr(media_store, name, original)
        action()
        return original(*args, **kwargs)

    monkeypatch.setattr(media_store, name, hooked)


def test_a_portrait_made_just_before_the_delete_keeps_the_file(host, monkeypatch):
    """Portrait first, then the delete: the delete's file check, under the same write lock,
    sees the portrait and keeps the file; the answer names it."""
    asset = host.publish(_png(1))
    aid, blob, results = asset["asset_id"], _blob(asset), []
    _before(monkeypatch, "delete", _add_portrait(aid, host.media_dir, results))
    status, out = host.call("DELETE", f"/media/{aid}")
    monkeypatch.undo()
    [made] = results
    assert isinstance(made, dict), made
    portrait = next(p for p in made["portraits"] if p["media_asset_id"] == aid)
    assert status == 200 and [u["kind"] for u in out["still_used_as"]] == ["portrait"]
    assert blob.is_file()
    status, content = host.call("GET", portrait["url"])
    assert status == 200 and content == _png(1)


def test_a_portrait_tried_between_the_mark_and_the_release_is_refused(host, monkeypatch):
    """The row is already deleted when the portrait is tried, the file not yet released: the
    portrait is refused in words and the release then removes the file."""
    asset = host.publish(_png(1))
    aid, blob, results = asset["asset_id"], _blob(asset), []
    _before(monkeypatch, "release_blob", _add_portrait(aid, host.media_dir, results))
    status, out = host.call("DELETE", f"/media/{aid}")
    monkeypatch.undo()
    [refused] = results
    assert isinstance(refused, agent_portraits.PortraitError), refused
    assert refused.status == 404 and str(refused) == "media asset not found"
    assert status == 200 and out["still_used_as"] == []
    assert not blob.exists()
    assert db.conn().execute("SELECT COUNT(*) FROM agent_portraits WHERE media_asset_id=?",
                             (aid,)).fetchone()[0] == 0
