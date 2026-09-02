---
name: clarp-location
description: Request the user's current location through the Clarp phone app. Use when a task genuinely requires current physical position.
---

# Clarp Location

Run `clarp-admin location --session "$CLARP_SESSION"`. The phone owns the
permission prompt and the user may decline. Reuse a sufficiently recent fix and
continue without location when approval is not provided.
