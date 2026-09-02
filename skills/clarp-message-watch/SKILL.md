---
name: clarp-message-watch
description: Watch for expected WhatsApp or email replies and wake a Clarp agent when matching messages arrive. Requires a configured provider CLI.
---

# Clarp Message Watch

This optional integration requires `wacli` for WhatsApp or `himalaya` for
email. Register one Clarp background job per watched target and use
`clarp-self-prompt` for matching deliveries. Outbound replies remain separate
external actions requiring the user's authorization. The shared message-watch
worker is an adopted lifecycle worker: registration captures its process
identity, its status heartbeat renews the exact target IDs encoded in its launch
arguments, every delivery checks the cancellation gate, and normal shutdown
finishes each target job.

Run the managed worker at
`clarp-message-watch`.
It stores the generation-specific handle returned for each target and fails
closed when registration or the cancellation gate is unavailable.
