"""One reader of a moment for countdown publishing and Updates (R104)."""
import pytest

from lib import artifacts, countdown_attention
from lib.moments import read_moment


@pytest.mark.parametrize("text", [
    "2026-11-22T24:00:00Z", "2026-11-22T24:00:00+01:00", "2026-11-22T23:60:00Z",
    "2026-11-22T23:59:60Z", "2026-11-22T12:00:00", "2026-02-30T12:00:00Z", "", 5, None])
def test_an_hour_of_24_and_every_other_bad_moment_are_refused(text):
    """24:00:00 read as the next midnight on Python 3.14 and was refused on older ones; it is
    refused on every interpreter now, with the out-of-range parts, a missing offset and an
    impossible date."""
    with pytest.raises(ValueError):
        read_moment(text)


def test_good_moments_read_as_before():
    assert read_moment("2026-11-22T23:59:59Z").isoformat() == "2026-11-22T23:59:59+00:00"
    assert read_moment("2026-11-22T00:00:00.250+05:30").utcoffset().total_seconds() == 19800


def test_publishing_and_updates_agree_on_a_midnight_written_as_24(tmp_path):
    """Each read target_at with its own parser, so what 24:00:00 meant followed the
    interpreter (on 3.14, the next midnight). Publishing now refuses it, and Updates reads a
    stored one as an invalid target, on every interpreter."""
    from lib import agents
    agents.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    with pytest.raises(ValueError, match="countdown target_at requires a valid date/time"):
        artifacts.create(session="mike", type="countdown", title="Deadline",
                         payload={"target_at": "2026-11-22T24:00:00Z", "time_zone": "UTC"})
    row = dict(server_id="host-a", artifact_id="event", occurrence_id="one", updated_at=1,
               status="ready", target_at="2026-11-22T24:00:00Z", purpose="informational")
    projected = countdown_attention.project(row, now_ms=0, policy="action", retention_hours=24)
    assert projected["phase"] == "invalid_target"
