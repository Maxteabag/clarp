---
name: clarp-calendar
description: Ask the Clarp iOS app to add an Apple Calendar event. Use when the user provides or approves concrete event details.
---

# Clarp Calendar

Use `clarp-admin calendar`. Require a title, start, end, and
timezone or explicit offset. Never invent a time. Server acceptance means the
request reached the phone; EventKit permission or validation can still fail on
the device.
