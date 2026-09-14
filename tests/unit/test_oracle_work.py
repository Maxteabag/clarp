from lib import oracle_work


def work(ident, trace, *, follow="", status="completed", result="Cancellation is unsupported."):
    return {"delegation_id": ident, "trace_id": trace, "completion_trace_id": follow,
            "session": "rowan", "backend_session_id": "same-conversation", "request_text": ident,
            "status": status, "result_text": result, "created_at": 1 if ident == "original" else 2}


def test_a_correction_is_one_job_with_two_retained_requests():
    rows = [work("original", "trace-1"), work("also-cancellation", "trace-2", follow="trace-1")]
    result = oracle_work.project(rows)
    assert len(result) == 1
    assert result[0]["id"] == "original"
    assert result[0]["operation_ids"] == ["original", "also-cancellation"]
    assert result[0]["requests"] == ["original", "also-cancellation"]


def test_same_agent_and_native_conversation_do_not_merge_independent_turns():
    rows = [work("original", "trace-1"), work("separate", "trace-2")]
    assert len(oracle_work.project(rows)) == 2


def test_pending_correction_does_not_show_old_result_as_completed_work():
    rows = [work("original", "trace-1"), work("correction", "trace-2", follow="trace-1", status="accepted", result="")]
    result = oracle_work.project(rows)[0]
    assert result["status"] == "accepted"
    assert result["result"] == ""
