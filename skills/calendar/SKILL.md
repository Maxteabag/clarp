---
name: calendar
description: Add events to the user's Apple Calendar through the Clarp iOS app. Use when the user asks to schedule, book, add to calendar, create a reminder-like calendar block, or when an agent has enough concrete event details to create a calendar event.
---

# Calendar

## Overview

Use this skill to ask the Clarp iOS app to create an Apple Calendar event. The server sends a `calendar-request` event to the phone; iOS owns the EventKit permission prompt and then writes future events automatically after the user grants access once.

## Required Details

Before sending a request, make sure you have:

- `session`: the target Clarp agent session that should receive the device event.
- `title`: concise event title.
- `start`: ISO 8601 datetime with offset, for example `2026-06-24T15:00:00+02:00`.
- `end`: ISO 8601 datetime with offset.

Ask the user in plain text if the title, start, end, or timezone cannot be inferred safely. Do not invent calendar times.

Optional fields:

- `location`
- `notes`
- `url`
- `time_zone`, for example `Europe/Oslo`
- `calendar`, when the user names a target calendar
- `--all-day`, for all-day events

## Request Event

Run the helper script:

```bash
python3 skills/calendar/scripts/request_calendar_event.py \
  --session "$CLARP_SESSION" \
  --title "Dentist" \
  --start "2026-06-24T15:00:00+02:00" \
  --end "2026-06-24T15:30:00+02:00" \
  --time-zone "Europe/Oslo" \
  --location "Oslo"
```

If `CLARP_SESSION` is not set, pass the current agent session explicitly. The helper defaults to `CLARP_SERVER_URL`, then `CLAUDE_PWA_URL`, then `http://127.0.0.1:8765`. It uses `CLARP_AUTH_TOKEN` or `CLAUDE_PWA_TOKEN` when present.

## Result Handling

The script returning `ok: true` means the server accepted and broadcast the request. The phone may still fail later if Calendar permission is denied, the calendar is not writable, or the date is invalid. Those failures are surfaced in the app diagnostics/status.
