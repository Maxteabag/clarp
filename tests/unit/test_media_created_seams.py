"""media-changed for images published outside POST /media (issue 489 follow-up): sent by the
Host after the commit, at the two seams where the Host learns of them, never inside
media_store.publish. Portrait generation: when its job ends, created for each image the run
kept, deleted for each it discarded. Podcast companion: created once save_image committed;
a save that rolls back sends nothing."""
from __future__ import annotations

import base64
import threading
from types import SimpleNamespace

import pytest

from lib import (agents, artifacts, background_job_watcher, background_jobs, db, media_store,
                 podcast_history, podcast_live, portrait_generation)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _media_events(sent):
    return [e for e in sent if e.get("type") == "media-changed"]


# ---- portrait generation --------------------------------------------------------

@pytest.fixture
def portrait_run(tmp_path):
    agents.create_agent(persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    agent_id = agents.get_by_session("lena")["agent_id"]
    job = background_jobs.upsert_computer(
        computer_id="host", job_id=portrait_generation.job_id_for(agent_id),
        kind="portrait-generation", title="Generate portraits", status="running",
        metadata={"session": "lena"})

    def publish(n, created_by=f"{portrait_generation.CREATED_BY_PREFIX}openai"):
        return media_store.publish(session="lena", blob=PNG + f"#{n}".encode(),
                                   source_name=f"p{n}.png", content_type="image/png",
                                   created_by=created_by, media_dir=tmp_path / "media")

    return SimpleNamespace(job=job, publish=publish, agent_id=agent_id)


def _watch_until_end(run, status, reason=""):
    sent: list[dict] = []
    watcher = background_job_watcher.BackgroundJobWatcher(SimpleNamespace(broadcast=sent.append))
    watcher._poll_once()            # the running job: nothing to announce
    assert _media_events(sent) == []
    background_jobs.finish(run.job["job_id"], generation=int(run.job["generation"]),
                           status=status, reason=reason)
    watcher._poll_once()
    watcher._poll_once()            # nothing new: nothing sent twice
    return _media_events(sent)


def test_a_finished_portrait_run_announces_the_images_it_kept(portrait_run):
    kept = [portrait_run.publish(1), portrait_run.publish(2)]
    portrait_run.publish(3, created_by="agent")  # the agent's own image: not the run's
    sent = _watch_until_end(portrait_run, "succeeded")
    assert sorted(e["asset_id"] for e in sent) == sorted(a["asset_id"] for a in kept)  # once each
    assert [(e["change"], e["asset_id"]) for e in sent] == sorted(
        [("created", a["asset_id"]) for a in kept],
        key=lambda c: (media_store.lookup(c[1])["created_at"], c[1]))
    for event in sent:
        row = media_store.lookup(event["asset_id"])
        assert event == {"type": "media-changed", "session": "lena", "agent_id": portrait_run.agent_id,
                         "asset_id": row["asset_id"], "change": "created", "at": row["created_at"]}


def test_a_failed_portrait_run_announces_the_images_it_discarded(portrait_run):
    staged = [portrait_run.publish(1), portrait_run.publish(2)]
    portrait_generation._discard_assets(staged)
    sent = _watch_until_end(portrait_run, "failed", "image provider failed")
    assert sorted(e["asset_id"] for e in sent) == sorted(a["asset_id"] for a in staged)  # once each
    assert sorted((e["change"], e["asset_id"]) for e in sent) == sorted(
        ("deleted", a["asset_id"]) for a in staged)
    assert all(e["at"] == media_store.lookup(e["asset_id"])["deleted_at"] for e in sent)


def test_a_later_event_of_the_finished_job_announces_nothing_again(portrait_run):
    """Announced on the job's change to a terminal status, not on every event it records: a
    later write to the finished job (here its owner reassigned) sends no second created."""
    kept = portrait_run.publish(1)
    sent: list[dict] = []
    watcher = background_job_watcher.BackgroundJobWatcher(SimpleNamespace(broadcast=sent.append))
    background_jobs.finish(portrait_run.job["job_id"], generation=int(portrait_run.job["generation"]),
                           status="succeeded")
    watcher._poll_once()
    assert [e["asset_id"] for e in _media_events(sent)] == [kept["asset_id"]]
    before = background_jobs.latest_event_id()
    assert background_jobs.reassign_terminal_computer_owner(
        portrait_run.job["job_id"], computer_id="host") is not None
    assert background_jobs.latest_event_id() > before  # a real new event of the finished job
    watcher._poll_once()
    assert [e["asset_id"] for e in _media_events(sent)] == [kept["asset_id"]]


def test_after_the_watcher_forgets_a_finished_run_a_later_event_announces_it_again(portrait_run):
    """The watcher remembers job states in a bounded map: past 1,024 entries it drops 256
    that are not active, oldest first. A finished run it has dropped looks new again, so a
    later event of that run announces its kept image once more: the documented idempotent
    hint (protocol.md, created per asset_id), exactly one repeat, nothing else."""
    kept = portrait_run.publish(1)
    sent: list[dict] = []
    watcher = background_job_watcher.BackgroundJobWatcher(SimpleNamespace(broadcast=sent.append))
    job_id = portrait_run.job["job_id"]
    background_jobs.finish(job_id, generation=int(portrait_run.job["generation"]), status="succeeded")
    watcher._poll_once()
    assert [e["asset_id"] for e in _media_events(sent)] == [kept["asset_id"]]
    assert next(iter(watcher._statuses)) == job_id           # the oldest entry
    for n in range(1024):                                   # other finished jobs it has seen
        watcher._statuses[f"other-{n}"] = (1, "succeeded")
    other = background_jobs.upsert_computer(computer_id="host", job_id="unrelated-job",
                                            kind="other", title="Unrelated", status="running")
    background_jobs.finish("unrelated-job", generation=int(other["generation"]), status="succeeded")
    watcher._poll_once()                                    # its transition trims the map
    assert job_id not in watcher._statuses
    assert [e["asset_id"] for e in _media_events(sent)] == [kept["asset_id"]]
    assert background_jobs.reassign_terminal_computer_owner(job_id, computer_id="host") is not None
    watcher._poll_once()
    assert [(e["change"], e["asset_id"]) for e in _media_events(sent)] == [
        ("created", kept["asset_id"]), ("created", kept["asset_id"])]


def test_images_published_before_the_run_started_are_not_its(portrait_run, tmp_path):
    earlier = portrait_run.publish(1)
    db.conn().execute("UPDATE media_assets SET created_at = created_at - 60000 WHERE asset_id = ?",
                      (earlier["asset_id"],))
    assert _watch_until_end(portrait_run, "succeeded") == []


def test_an_older_portrait_image_deleted_during_the_run_is_not_the_runs(portrait_run):
    """The run's discarded set is the images it published and discarded, not every image of
    its kind deleted while it ran: an older generated portrait image a device deletes during
    the run gets its own deleted from that delete, never a second one from the run."""
    older = portrait_run.publish(1)
    db.conn().execute("UPDATE media_assets SET created_at = created_at - 60000 WHERE asset_id = ?",
                      (older["asset_id"],))
    own = portrait_run.publish(2)
    media_store.delete(older["asset_id"], deleted_by="device_x")  # deleted while the run goes
    portrait_generation._discard_assets([own])
    sent = _watch_until_end(portrait_run, "failed", "image provider failed")
    assert [(e["change"], e["asset_id"]) for e in sent] == [("deleted", own["asset_id"])]


# ---- the podcast companion -------------------------------------------------------

@pytest.fixture
def podcast(tmp_path):
    agents.create_agent(persona="Reader", voice_id="V", cwd=str(tmp_path), session="reader")
    audio = media_store.publish(session="reader", blob=b"ID3\x04\x00\x00audio",
                                source_name="episode.mp3", content_type="audio/mpeg",
                                media_dir=tmp_path / "media")
    episode = {"revision": media_store.get(audio["asset_id"])["sha256"],
               "transcript": [{"start": 0, "end": 10, "text": "Original passage"}],
               "chapters": [{"start": 0, "end": 10, "title": "Chapter", "source": "Result"}]}
    artifact = artifacts.create(session="reader", type="audio", title="The episode",
                                payload={"url": audio["url"], "mime_type": "audio/mpeg",
                                         "file_name": "episode.mp3", "duration_ms": 10000,
                                         "podcast": episode})
    context = podcast_live.context_for(episode, 5, 10)
    ident = podcast_history.create(artifact=artifact, position=5, context=context,
                                   model="gpt-live-1.1", voice="marin")
    return SimpleNamespace(ident=ident, media_dir=tmp_path / "media")


class _Detour(podcast_live._PodcastDetour):
    """The podcast behaviour without a live connection: _image as a conversation runs it."""

    def __init__(self, *, stream, history_id, media_dir):
        jpeg = base64.b64encode(b"\xff\xd8\xff concept image").decode()
        self._start_detour({}, True, lambda **_kw: jpeg, history_id, media_dir,
                           lambda: 0.0, stream)
        self.stop, self.lock, self.revision, self.api_key = threading.Event(), threading.Lock(), 1, "k"
        self.downstream_sent: list[dict] = []
        self.downstream = self.downstream_sent.append


def test_a_saved_podcast_image_is_announced_after_its_commit(podcast):
    sent: list[dict] = []
    detour = _Detour(stream=SimpleNamespace(broadcast=sent.append),
                     history_id=podcast.ident, media_dir=podcast.media_dir)
    detour._image("Please illustrate the passage", 1)
    [shown] = [e for e in detour.downstream_sent if e["type"] == "podcast.image"]
    [event] = _media_events(sent)
    row = media_store.lookup(shown["asset_id"])
    assert event == {"type": "media-changed", "session": "reader", "agent_id": row["agent_id"],
                     "asset_id": row["asset_id"], "change": "created", "at": row["created_at"]}


def test_each_saved_podcast_image_is_announced_exactly_once(podcast):
    sent: list[dict] = []
    detour = _Detour(stream=SimpleNamespace(broadcast=sent.append),
                     history_id=podcast.ident, media_dir=podcast.media_dir)
    detour._image("Please illustrate the passage", 1)
    detour.revision = 2
    detour.image_pending = False
    detour._image("And now the second idea, please", 2)
    shown = [e["asset_id"] for e in detour.downstream_sent if e["type"] == "podcast.image"]
    assert len(shown) == 2 and len(set(shown)) == 2
    assert sorted(e["asset_id"] for e in _media_events(sent)) == sorted(shown)


def test_a_podcast_save_that_rolls_back_sends_nothing(podcast, monkeypatch):
    """The image row is written inside the podcast's transaction, then the save fails: the
    row rolls back with it and no event names it."""
    written: list[str] = []
    publish = media_store.publish

    def publish_then_fail(**kwargs):
        written.append(publish(**kwargs)["asset_id"])
        raise RuntimeError("disk full after the row")

    monkeypatch.setattr(media_store, "publish", publish_then_fail)
    sent: list[dict] = []
    detour = _Detour(stream=SimpleNamespace(broadcast=sent.append),
                     history_id=podcast.ident, media_dir=podcast.media_dir)
    detour._image("Please illustrate the passage", 1)
    assert [e["type"] for e in detour.downstream_sent] == ["podcast.image_failed"]
    assert _media_events(sent) == []
    assert written and media_store.lookup(written[0]) is None


def test_every_podcast_conversation_is_given_the_hosts_stream():
    """The three places that open a podcast conversation hand it the Host's stream."""
    import inspect
    import re
    from lib import oracle_live, oracle_live_calls, oracle_live_stable
    for module in (oracle_live, oracle_live_calls, oracle_live_stable):
        source = inspect.getsource(module)
        calls = [m.start() for m in re.finditer(r"PodcastConversation\(", source)]
        assert calls, module.__name__
        for at in calls:
            # The call's own arguments: up to the first line that closes it.
            end = source.index(")\n", source.index("stream=", at) if "stream=" in source[at:at + 900] else at)
            assert 'stream=getattr(' in source[at:end + 1], (module.__name__, source[at:at + 200])
