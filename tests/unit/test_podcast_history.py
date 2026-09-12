import base64
import json
from types import SimpleNamespace

import pytest

from lib import agents, artifacts, db, media_store, podcast_history as history, podcast_live


@pytest.fixture
def saved(tmp_path):
    agents.create_agent(persona="Reader", voice_id="V", cwd=str(tmp_path), session="reader")
    audio = media_store.publish(session="reader", blob=b"ID3\x04\x00\x00audio",
        source_name="episode.mp3", content_type="audio/mpeg", media_dir=tmp_path/"media")
    episode = {"revision": media_store.get(audio["asset_id"])["sha256"],
        "transcript": [{"start": 0, "end": 10, "text": "Original passage"}],
        "chapters": [{"start": 0, "end": 10, "title": "Original chapter", "source": "Measured result"}]}
    artifact = artifacts.create(session="reader", type="audio", title="The episode",
        payload={"url": audio["url"], "mime_type": "audio/mpeg", "file_name": "episode.mp3",
                 "duration_ms": 10000, "podcast": episode})
    source = artifacts.create(session="reader", type="document", title="Original plan",
        payload={"content": "Original budget is a proposal."})
    context = podcast_live.context_for(episode, 5, 10, source=source)
    ident = history.create(artifact=artifact, position=5, context=context, source=source,
                           model="gpt-live-1", voice="marin")
    return ident, artifact, source


def delta(ident, text, role="user"):
    return history.record(ident, {"type": "session."+("input" if role=="user" else "output")+"_transcript.delta",
                                  "delta": text, "start_ms": 20, "end_ms": 200})


def test_context_text_and_configuration_survive_source_edits_and_reopen(saved):
    ident, episode, source = saved
    text = "Question with literal 10% _ marks. " * 200
    delta(ident, text)
    delta(ident, "The full answer.", "assistant")
    artifacts.update(source["artifact_id"], {"title": "Changed plan", "payload": {"content": "New budget"}})
    history.finish(ident)
    db.conn().close()
    db._LOCAL.conn = None
    value = history.get(ident)
    assert value["source"]["title"] == "Original plan"
    assert value["source"]["payload"]["content"] == "Original budget is a proposal."
    assert value["context"]["paused_seconds"] == 5
    configuration = value["model_configuration"]
    assert configuration["instructions"] == podcast_live.PROMPT
    assert json.loads(configuration["input"][0]["content"][0]["text"].split("\n", 1)[1]) == value["context"]
    assert "".join(e["text"] for e in value["events"] if e["role"] == "user") == text
    assert value["events"][-1]["text"] == "The full answer."
    assert value["status"] == "closed"
    assert history.search(text="10% _")["conversations"][0]["conversation_id"] == ident


def test_full_transcript_pagination_and_process_interruption(saved, monkeypatch):
    ident, *_ = saved
    for word in ["First ", "second ", "third ", "fourth"]: delta(ident, word)
    first = history.get(ident, limit=2)
    delta(ident, " A new live update")
    second = history.get(ident, after_event_id=first["next_event_id"], limit=2,
                         through_event_id=first["through_event_id"])
    assert "".join(e["text"] for e in first["events"]+second["events"]) == "First second third fourth"
    assert second["next_event_id"] is None
    monkeypatch.setattr(history, "INSTANCE_ID", "a-new-host-process")
    assert history.get(ident)["status"] == "interrupted"
    assert history.search()["conversations"][0]["status"] == "interrupted"
    assert history.get(ident)["closed_at"] is None


def test_image_is_permanent_and_linked_to_question(saved, tmp_path):
    ident, *_ = saved
    event_id = delta(ident, "Please illustrate this")
    blob = b"\xff\xd8\xfffixture"
    receipt = history.save_image(ident, encoded=base64.b64encode(blob).decode(),
        question="Please illustrate this", question_event_id=event_id, media_dir=tmp_path/"media")
    asset = media_store.get(receipt["asset_id"])
    assert __import__("pathlib").Path(asset["storage_path"]).read_bytes() == blob
    history.finish(ident)
    image = history.get(ident)["images"][0]
    assert image["asset_id"] == receipt["asset_id"]
    assert image["question_event_id"] == event_id
    assert "storage_path" not in image


def test_feedback_freezes_which_images_were_included(saved, tmp_path):
    ident, _, source = saved
    event = delta(ident, "Explain this picture")
    def save_image():
        return history.save_image(ident, encoded=base64.b64encode(b"\xff\xd8\xfffixture").decode(),
            question="Explain this picture", question_event_id=event, media_dir=tmp_path/"media")
    first = save_image()
    data = {"feedback_id":"feedback-image-list-123", "target_artifact_id":source["artifact_id"],
        "target_updated_at":source["updated_at"], "through_event_id":event, "image_ids":[first["image_id"]]}
    receipt = history.feedback(ident, data)
    second = save_image()
    value = history.get(ident)
    assert len(value["images"]) == 2
    assert value["feedback"][0]["image_ids"] == [first["image_id"]]
    assert history.feedback(ident, data) == receipt
    with pytest.raises(ValueError,match="different feedback"):
        history.feedback(ident,{**data,"image_ids":[second["image_id"]]})


def test_explicit_feedback_is_idempotent_frozen_and_does_not_dispatch(saved):
    ident, _, source = saved
    event = delta(ident, "I prefer the smaller budget")
    data = {"feedback_id": "feedback-stable-id-123", "target_artifact_id": source["artifact_id"],
            "target_updated_at": source["updated_at"], "through_event_id": event, "note": "Consider this when revising"}
    receipt = history.feedback(ident, data)
    assert receipt["saved"] and receipt["agent_dispatched"] is False
    artifacts.update(source["artifact_id"], {"payload": {"content": "A later plan"}})
    assert history.feedback(ident, data) == receipt
    with pytest.raises(ValueError, match="target changed"):
        history.feedback(ident, {**data, "feedback_id": "feedback-new-version-123"})
    assert history.get(ident)["feedback"][0]["target"]["payload"]["content"] == "Original budget is a proposal."
    assert len(history.search(source_artifact_id=source["artifact_id"], feedback_only=True)["conversations"]) == 1
    with pytest.raises(ValueError, match="different feedback"):
        history.feedback(ident, {**data, "note": "Changed"})
    with pytest.raises(ValueError, match="unsaved event"):
        history.feedback(ident, {**data, "feedback_id": "feedback-another-123", "through_event_id": event+100})


def test_save_failure_stops_forwarding_transcript(saved, monkeypatch):
    ident, *_ = saved
    sent=[]
    conversation=podcast_live.PodcastConversation(SimpleNamespace(send=lambda _:None), sent.append,
        "fixture", "{}", history_id=ident)
    monkeypatch.setattr(history, "record", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
    try:
        with pytest.raises(OSError):
            conversation.receive({"type":"session.input_transcript.delta", "delta":"Unsaved"})
        assert conversation.stop.is_set()
        assert [e["type"] for e in sent] == ["podcast.history_error"]
    finally:
        conversation.pool.shutdown(wait=True)


def test_sources_http_and_full_device_boundary(saved):
    from lib import podcast_history_http
    ident, _, source = saved
    handler=SimpleNamespace(path="/podcast-history/sources?search=Original", _request_auth_validated=True,
        _request_device_scope="full", _request_principal="test", _send=lambda status,body,mime:(status,json.loads(body)))
    status, body=podcast_history_http.handle(handler, "GET")
    assert status == 200 and body["sources"][0]["artifact_id"] == source["artifact_id"]
    handler.path="/podcast-history/"+ident
    assert podcast_history_http.handle(handler,"GET")[1]["conversation_id"] == ident
    handler._request_device_scope="limited"
    assert podcast_history_http.handle(handler,"GET")[0] == 403


@pytest.mark.parametrize("old_version", [80, 81, 82])
def test_upgrade_preserves_old_voice_events_and_adds_history(tmp_path, old_version):
    con=db.conn()
    from lib import voice_events
    event=voice_events.record("transcript", text="Existing speech")
    for table in ("podcast_feedback", "podcast_images", "podcast_conversations", "podcast_snapshots"):
        con.execute("DROP TABLE "+table)
    con.execute("CREATE INDEX fixture_existing_voice_index ON voice_events(session)")
    con.execute(f"PRAGMA user_version={old_version}")
    db._migrate(con)
    assert con.execute("SELECT text FROM voice_events WHERE event_id=?",(event,)).fetchone()[0] == "Existing speech"
    assert con.execute("SELECT count(*) FROM podcast_conversations").fetchone()[0] == 0
    assert con.execute("PRAGMA user_version").fetchone()[0] == 83
    assert con.execute("SELECT name FROM sqlite_master WHERE name='fixture_existing_voice_index'").fetchone()


def test_voice_connection_persists_context_before_provider_and_closes(saved, tmp_path, monkeypatch):
    import io
    import queue
    import threading
    import websocket
    from urllib.parse import urlencode
    from lib import oracle_live, ws
    _, artifact, source = saved
    answer_sent = threading.Event()
    output = []
    incoming = queue.Queue()
    class Upstream:
        def settimeout(self, value): pass
        def close(self): pass
        def recv(self):
            try: return incoming.get(timeout=.2)
            except queue.Empty: raise websocket.WebSocketTimeoutException()
        def send(self, raw):
            event=json.loads(raw)
            if event["type"] == "session.start":
                assert history.search()["conversations"][0]["status"] == "connecting"
                for event in [{"type":"session.started"},
                    {"type":"session.input_transcript.delta","delta":"What does this mean?"},
                    {"type":"session.output_transcript.delta","delta":"It is a proposal."}]:
                    incoming.put(json.dumps(event))
            if event["type"] == "session.close": incoming.put(json.dumps({"type":"session.closed"}))
    class Writer:
        def write(self, blob):
            if not blob.startswith(b"HTTP/"):
                # The production reader requires masked client frames. Decode
                # server output with the real browser-side library instead.
                frame=websocket._abnf.frame_buffer(io.BytesIO(blob).read, False).recv_frame()
                if frame.opcode==ws.OP_TEXT:
                    event=json.loads(frame.data);output.append(event)
                    if event.get("type")=="session.output_transcript.delta":answer_sent.set()
        def flush(self): pass
    upstream=Upstream()
    def connect(*args,**kwargs):
        assert len(history.search()["conversations"]) == 2
        return upstream
    monkeypatch.setattr(websocket,"create_connection",connect)
    monkeypatch.setattr(oracle_live.config,"load",lambda:SimpleNamespace(openai_key=lambda:"fixture"))
    original_read=ws.read_frame
    # Only the client's rfile is injected. Outgoing frames use the real parser.
    marker=object()
    def read(stream):
        if stream is not marker:return original_read(stream)
        assert answer_sent.wait(2)
        return ws.OP_CLOSE,b""
    monkeypatch.setattr(ws,"read_frame",read)
    handler=SimpleNamespace(headers={"Upgrade":"websocket","Connection":"Upgrade",
        "Sec-WebSocket-Key":"dGhlIHNhbXBsZSBub25jZQ=="}, _request_principal="history-test-client",
        _request_auth_validated=True,_request_device_scope="full",ctx=SimpleNamespace(media_dir=tmp_path/"media"),
        path="/oracle/v2?"+urlencode({"podcast_artifact":artifact["artifact_id"],
            "revision":artifact["podcast"]["revision"],"position":"5","source_artifact":source["artifact_id"]}),
        wfile=Writer(),rfile=marker,connection=SimpleNamespace(settimeout=lambda _:None,shutdown=lambda _:None))
    oracle_live.serve(handler)
    ident=next(e["conversation_id"] for e in output if e["type"]=="podcast.history")
    value=history.get(ident)
    assert [e["text"] for e in value["events"]] == ["What does this mean?","It is a proposal."]
    assert value["status"] == "closed"
    assert value["context"]["linked_source"]["title"] == "Original plan"
    assert all(e.get("history_event_id") for e in output if e["type"].endswith("transcript.delta"))


def test_agent_helper_follows_frozen_transcript_pages(saved):
    import importlib.util
    import pathlib
    import sys
    from urllib.parse import parse_qs,urlparse
    scripts=pathlib.Path(__file__).resolve().parents[2]/"scripts"
    sys.path.insert(0,str(scripts))
    spec=importlib.util.spec_from_file_location("podcast_history_cli",scripts/"podcast_history.py")
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    ident,*_=saved
    for text in ["My ","whole ","question."]:delta(ident,text)
    calls=[]
    def request(method,path):
        calls.append(path)
        q=parse_qs(urlparse(path).query)
        return history.get(ident,limit=1,after_event_id=int(q.get("after_event_id",[0])[0]),
                           through_event_id=int(q["through_event_id"][0]) if "through_event_id" in q else None)
    value=cli.full_conversation(ident,request=request)
    assert "".join(e["text"] for e in value["events"])=="My whole question."
    assert len(calls)==3 and value["next_event_id"] is None
