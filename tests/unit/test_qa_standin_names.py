"""The QA stand-ins write a provider's file under the name the Host's own lookup takes, so the two
cannot drift apart again. #434 (R70) made the Codex lookup take only Codex's real rollout name,
rollout-<YYYY-MM-DDTHH-MM-SS>-<thread id>.jsonl; the stand-in still wrote rollout-<thread id>.jsonl,
so after a Host restart its thread was 'not on disk' and the agent started fresh
(test_qa_turns::test_durable_queue_survives_host_restart, red since). The matchers are imported from
the Host, never copied."""
from __future__ import annotations

import uuid

from lib import codex_transcript, transcript_paths
from tests.qa.fake_codex import rollout_path


def test_the_codex_stand_ins_rollout_is_named_as_the_hosts_lookup_takes_it(tmp_path):
    thread = str(uuid.uuid4())
    path = rollout_path(tmp_path, thread)
    named = codex_transcript._ROLLOUT_NAME.fullmatch(path.name)
    assert named is not None and named.group(1) == thread


def test_the_hosts_lookup_finds_the_codex_stand_ins_rollout(tmp_path):
    thread = str(uuid.uuid4())
    path = rollout_path(tmp_path, thread)
    path.write_text("{}\n")
    assert codex_transcript.find_latest_jsonl(thread, sessions_root=tmp_path) == path


def test_a_threads_rollout_keeps_its_name_once_written(tmp_path):
    thread = str(uuid.uuid4())
    first = rollout_path(tmp_path, thread)
    first.write_text("{}\n")
    assert rollout_path(tmp_path, thread) == first


def test_the_claude_stand_ins_session_id_passes_the_hosts_rule(tmp_path):
    """tests/integration/fake_claude.py writes <session id>.jsonl, Claude's own file name, and binds
    that id through the hooks: the shared rule every binding and every Claude lookup applies."""
    from tests.integration.fake_claude import FakeClaude
    claude = FakeClaude(home=tmp_path, session="mike")
    assert transcript_paths.session_id_ok(claude.backend_session_id)
    assert claude.transcript.name == f"{claude.backend_session_id}.jsonl"
