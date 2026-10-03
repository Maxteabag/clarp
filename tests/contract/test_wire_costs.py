"""What the wire costs: byte budgets and cache headers on the real server.

Like T3 Code's `test:perf:v2-wire`, these pin how many bytes a client pays
for the common paths, so a change that quietly fattens every page (a new
per-row field, a duplicated body, lost compression) fails here instead of
showing up as a slower phone weeks later. The budgets are measured values
plus headroom; raise one only with a reason in the commit.

- A /log tail page costs its message text plus a bounded envelope per row,
  and gzip keeps the page well under its raw size.
- A live assistant row that grows by a few words costs one row in the delta,
  not the page.
- The web app's own assets are cached by content hash; the shell and every
  unversioned request revalidate with an ETag (304), API JSON stays no-store.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import re
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import message_live  # noqa: E402

# Bytes per row on top of the row's own JSON-escaped text (ids, timestamps,
# role, revision, provenance and the empty tools/cells lists). Measured at
# 399 bytes in October 2026, about half of it empty provenance strings.
LOG_ROW_ENVELOPE = 480
# Fixed bytes per /log response (cursor, conversation id, flags, cwd).
LOG_PAGE_ENVELOPE = 600
# Gzipped tail page relative to its raw JSON. The fixture's prose repeats, so
# this checks that the page is compressed (0.04 measured), not a prose ratio.
LOG_GZIP_RATIO = 0.25

PROSE = ("The pacing change coalesces partial text on the Host so each client "
         "applies at most a few updates a second instead of one per token. ")


def _request(base: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _bind(n_rows: int) -> str:
    row = agents_db.conn().execute(
        "SELECT agent_id FROM agents WHERE session = 'rachel'").fetchone()
    agent_id = str(row["agent_id"])
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, "backend-1")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/wire.jsonl",
        turns=[{"role": "user" if i % 2 == 0 else "assistant",
                "text": f"{i}: " + PROSE * (1 + i % 5),
                "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"}
               for i in range(n_rows)])
    return agent_id


def _text_bytes(turns: list[dict]) -> int:
    return sum(len(json.dumps(t["text"])) for t in turns)


def test_log_tail_page_costs_its_text_plus_a_bounded_envelope(core_server):
    base = core_server["base"]
    _bind(150)
    status, headers, raw = _request(base, "/log?session=rachel&limit=100")
    assert status == 200 and "Content-Encoding" not in headers
    page = json.loads(raw)
    assert len(page["turns"]) == 100 and page["has_more"] is True
    overhead = len(raw) - _text_bytes(page["turns"])
    assert overhead <= LOG_PAGE_ENVELOPE + 100 * LOG_ROW_ENVELOPE, (
        f"{overhead / 100:.0f} bytes of envelope per row")

    status, headers, packed = _request(base, "/log?session=rachel&limit=100",
                                       {"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") == "gzip"
    assert gzip.decompress(packed) == raw
    assert len(packed) <= LOG_GZIP_RATIO * len(raw), f"{len(packed)}/{len(raw)}"


def test_a_growing_live_row_costs_one_row_in_the_delta(core_server):
    base = core_server["base"]
    agent_id = _bind(150)
    _, _, raw = _request(base, "/log?session=rachel&limit=100")
    cursor = json.loads(raw)["latest_revision"]

    text = PROSE * 12
    for _ in range(3):
        text += "More words arrive. "
        message_live.upsert_live_assistant_message(
            agent_id=agent_id, backend_session_id="backend-1", text=text)
        status, _, raw = _request(
            base, f"/log?session=rachel&limit=100&after_revision={cursor}")
        assert status == 200
        delta = json.loads(raw)
        assert [t["role"] for t in delta["turns"]] == ["assistant"]
        assert len(raw) <= len(json.dumps(text)) + LOG_PAGE_ENVELOPE + LOG_ROW_ENVELOPE
        cursor = delta["latest_revision"]


def test_app_assets_are_cached_by_content_and_the_shell_revalidates(core_server):
    base = core_server["base"]
    status, headers, body = _request(base, "/")
    assert status == 200
    assert headers["Cache-Control"] == "no-cache" and headers.get("ETag")
    status, _, empty = _request(base, "/", {"If-None-Match": headers["ETag"]})
    assert status == 304 and empty == b""

    pinned = re.findall(r'src="(/static/lib/marked\.min\.js\?v=[0-9a-f]+)"', body.decode())
    assert pinned, body[:400]
    status, headers, script = _request(base, pinned[0])
    assert status == 200 and script
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"

    # The same file without (or with a stale) version revalidates instead.
    for url in ["/static/lib/marked.min.js", "/static/lib/marked.min.js?v=0000"]:
        status, headers, _ = _request(base, url)
        assert status == 200 and headers["Cache-Control"] == "no-cache", url
        status, _, empty = _request(base, url, {"If-None-Match": headers["ETag"]})
        assert status == 304 and empty == b"", url

    status, headers, _ = _request(base, "/styles.css")
    assert status == 200 and headers["Cache-Control"] == "no-cache"

    status, headers, _ = _request(base, "/agents/snapshot")
    assert status == 200 and headers["Cache-Control"] == "no-store"
