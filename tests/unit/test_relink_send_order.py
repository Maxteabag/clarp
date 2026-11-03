"""RM-N5 review (rf-readmodel): relinking the older of two equal client rows
must not change which transcript copy each one links to. The import matches
copies to client rows in send order (updated_at), so a relink bumps the
revision but leaves updated_at alone."""
from __future__ import annotations

from lib import db, message_store
from tests.readmodel import test_store_txn_characterization as t
from tests.readmodel.test_store_txn_characterization import clock  # noqa: F401


def test_a_relink_keeps_the_send_order(clock):
    t.client("c0", "continue", at=0)
    t.client("c3", "continue", at=60, origin="agent", sender=t.SENDER, trace="tr-3")
    clock.ms = t.BASE_MS + 120_000          # the relink happens later
    message_store.relink_client_message("c0", trace_id="tr-0b")
    db.conn().commit()

    t.store([t.u("continue", 0.2), t.a("first", 5), t.u("continue", 60.2), t.a("second", 65)])

    replies = [r for r in t.transcript_rows() if r["role"] == "assistant"]
    assert t.view(replies, "text", "origin", "sender_agent_id", "trace_id") == [
        ("first", "user", "", "tr-0b"), ("second", "agent", t.SENDER, "tr-3")]
