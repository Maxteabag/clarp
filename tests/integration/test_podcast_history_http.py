from tests.integration.test_attention_questions_http import host, _request
from tests.unit.test_podcast_history import saved
from lib import podcast_history as history


def test_real_http_history_and_feedback_keep_context_without_dispatch(host, saved):
    ident, artifact, source = saved
    event = history.record(ident, {"type":"session.input_transcript.delta", "delta":"Please consider a smaller pilot"})
    code, page = _request(host, "/podcast-history?artifact_id="+artifact["artifact_id"])
    assert code == 200 and page["conversations"][0]["conversation_id"] == ident
    code, detail = _request(host, "/podcast-history/"+ident)
    assert code == 200 and detail["source"]["title"] == "Original plan"
    assert detail["events"][0]["text"] == "Please consider a smaller pilot"
    body = {"feedback_id":"http-podcast-feedback-123", "target_artifact_id":source["artifact_id"],
        "target_updated_at":source["updated_at"], "through_event_id":event, "note":"Keep scope bounded", "image_ids":[]}
    code, receipt = _request(host, "/podcast-history/"+ident+"/feedback", body)
    assert code == 200 and receipt["saved"] and not receipt["agent_dispatched"]
    assert _request(host, "/podcast-history/"+ident+"/feedback", body)[1] == receipt
    assert host.deliveries == []
    assert _request(host, "/podcast-history/sources?search=Original")[1]["sources"][0]["artifact_id"] == source["artifact_id"]


def test_real_http_history_requires_authentication(host, saved):
    ident, *_ = saved
    assert _request(host, "/podcast-history", authenticated=False)[0] == 401
    assert _request(host, "/podcast-history/"+ident, authenticated=False)[0] == 401
    assert _request(host, "/podcast-history/"+ident+"/feedback", {}, authenticated=False)[0] == 401
