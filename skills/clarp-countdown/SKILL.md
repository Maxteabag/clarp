---
name: clarp-countdown
description: Publish a general-purpose live countdown artifact to any concrete event, launch, deadline, journey, appointment, delivery or other target date/time.
---
# Countdown artifacts

Use the existing authenticated artifact helper for a concrete target, not a
parallel page, recurring prompt or background timer:

```bash
clarp-agent-artifacts create ACTUAL_SESSION countdown "Project launch" \
  "Final preparation window" \
  '{"target_at":"2026-10-01T10:00:00+02:00","time_zone":"Europe/Oslo","content":"Optional useful detail."}'
```

`target_at` is an ISO 8601 date/time with an explicit UTC offset (`Z` is valid).
`time_zone` is an IANA zone used to display that instant. Title, summary and
optional Markdown content are generic; no use case has special schema fields.
Do not publish an invented target when the source only provides an estimate.
When converting an ambiguous local time around a DST change, resolve the intended
instant rather than silently choosing an offset.

The client calculates remaining time from the saved target and its current clock,
so it resumes correctly without a running agent or per-second server writes.
Before the target show remaining time; during the first minute show Target reached /
Now; afterwards show elapsed time as Since target. Never show a negative countdown
or imply that reaching a time proves the event happened. Cancellation remains an
explicit artifact status. The countdown does not schedule notifications or actions.

Use `clarp-agent-artifacts update ID ready '{"target_at":"...","time_zone":"..."}'`
to change the payload through the normal lifecycle, preserving identity. Include
the complete desired payload when using this command because it replaces payload
fields. Read back the artifact to verify the timestamp, zone and status. Do not
recreate duplicates merely because an upload or create response was ambiguous.
