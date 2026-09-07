# Retained message audio

`GET /clips/message?session=<session>&message_id=<canonical-message-id>` returns
an `events` array of ordinary audio-event descriptors for a saved assistant
message. It requires the normal Host authentication. The message and every
returned clip must belong to the requested agent.

The lookup extracts the message's spoken regions, uses the same TTS chunking
and normalization as synthesis, and finds the closest matching retained queue
entries. Persona prefixes are accounted for. Multiple chunks stay in order and
within their original trace when one is available. Already-played clips are
eligible. PCM clips retain their stream URL and format metadata.

The endpoint does not synthesize, broadcast or reorder any device's queue.
Clients can use its result for an explicit local replay, bypassing their normal
SSE age/deduplication fences for that user action only. A missing message,
missing segment or expired file returns 404 rather than a partial replay.
Malformed/missing query parameters return 400.

`message_audio_replay` in `/server-info` advertises the endpoint. Clients should
only expose the replay action when the connected Host advertises it.

Completed MP3 replies remain eligible for replay for up to seven days. The
janitor protects at most 256 MiB of the newest completed reply recordings beyond
the normal delivery retention window. Voice previews and herald announcements
retain normal expiry; deleted agents are not protected. Existing retention of
other audio formats is unchanged. Metadata lookup failure skips a cleanup pass
rather than deleting the replay cache. Audio already expired before this feature
cannot be reconstructed by this endpoint.
