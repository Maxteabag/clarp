# Oracle WebRTC migration contract

Media: native iPhone WebRTC audio to OpenAI. Host creates the call using authenticated SDP exchange and owns the sideband connection. Phone never receives the permanent API key. Host owns the effective prompt, tools and durable agent-result delivery. Realtime owns VAD and media interruption/truncation.

Preserve: selected HTTPS Host, existing agents/backends/personas, list/delegate/status/cancel/read messages, durable work and follow-ups, replay-safe admission, transcript diagnostics, manual/hands-free modes, background agent execution when voice stops, attribution and explicit cancellation. Old WebSocket endpoint stays for already installed clients during rollout.

Delivery invariants: no announcement while user speech, automatic user response, tool completion, or earlier audio is pending. One final message produces at most one automatic announcement per voice session even when shared by several delegations. Cancelled/incomplete announcements remain in history and never enter an automatic regeneration loop. ACK only after completed response AND output audio stopped without clearing. Preserve response/item IDs and cancellation reasons. Explicit user request can read the result again.

Tools run outside the socket read loop. A tool acceptance means saved/routed, not work completion. A follow-up uses existing backend steering and preserves durable result ownership. Do not retry uncertain agent commands on voice reconnection.

Connection attempts: client-owned generation; bounded transient retries, cancel immediately on End/mode change. Stable attempt ID prevents duplicate paid sessions; Host rejects reuse with different SDP and closes superseded calls. Classify missing credentials/permission separately. Capability includes WebRTC availability so older Hosts get an explanatory fallback choice.

Oracle contact concept: Realtime is the conversational voice; a chosen ordinary durable contact is its investigative fallback. Named target wins. Ambiguous/unknown work such as "who made it blue?" goes to the selected Oracle contact to investigate, without choosing an arbitrary project agent. Offer an existing contact selector and normal profile/history navigation. Persona concepts to discuss: a patient Archivist, a practical Navigator, a skeptical Sage. Persona flavor must not invent knowledge, permissions or tool capabilities. Keep a small tool surface; richer investigation belongs to the ordinary contact.

Verification: offline event-sequence TDD first, including real production controller with in-memory dependencies. Paid verification is manual only, excluded from CI, with a shared $10 ceiling, explicit per-probe reservations and cost evidence. First live probe is minimal WebRTC + one simulated tool; then actual app integration. Prior instruction prohibiting local Mac tests remains in force unless Peter changes it; do not claim native build or physical audio verification from Linux tests.

Sources checked: https://developers.openai.com/api/docs/guides/realtime-webrtc ; https://developers.openai.com/api/docs/guides/realtime-server-controls ; https://developers.openai.com/api/docs/guides/realtime-conversations ; https://developers.openai.com/api/docs/guides/realtime-vad .
