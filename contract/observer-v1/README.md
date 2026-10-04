# Read-only observer contract v1

A minimal privacy-filtered projection of the existing Host snapshot/SSE surfaces. This is a contract for adapters, not a new Host endpoint, provider tracker or SDK. Host tracking and lifecycle remain authoritative.

Identity is `(host_id, agent_id)`; session is routing metadata, name is a label. Times are integer epoch milliseconds, not seconds or animation time. A timestamp of zero means unavailable, never proof of freshness. Lifecycle and tool activity carry separate timestamps. A tool finishing does not end the turn. Unknown states remain unknown; never infer Idle from silence, unknown vocabulary or a disconnected transport.

Wire input: GET /server-info provides server_id; GET /agents/snapshot seeds current state; GET /events delivers JSON type-dispatched events with a durable cursor where available. The projected DTO includes only identity, name, lifecycle/alive/busy and generic activity category/status. Do not forward raw commands, summaries, paths, prompts, transcripts, voice payloads or arbitrary URLs. No writes, acknowledgments or focus changes belong to this contract.

Connect the stream before fetching the snapshot; buffer a bounded number of events during seeding and merge by per-agent state/activity timestamps. Equal timestamps are allowed for the state/activity pair from a single Host transition. Deduplicate transport IDs without treating the two envelopes as two completed tasks. A known terminal/newer lifecycle suppresses older running activity. Ignore older snapshots for an existing identity's state but reconcile roster membership only from a current successful snapshot.

Reconnect with cursor and refetch the snapshot. Cursor gaps alone are not proof of missing observer data: filtering skips unrelated events. A heartbeat may advance the cursor without revealing those payloads. This is not an atomic snapshot+cursor protocol or unlimited replay. Buffer overflow, unknown identity or roster events trigger resnapshot. A successful connected heartbeat prevents quiet-but-connected agents from being marked idle or disconnected. Actual stream loss marks all cached agents stale immediately. No new clock is invented for old data on each render frame.

Known busy lifecycle states: thinking/tool/compacting. Nonbusy: idle/done/stopped/waiting/interrupted/background/spawned. Unknown values are compatible but displayed generically. Connected explicit idle/done/stopped can use a relaxed avatar; stale/waiting/interrupted/background/unknown cannot.

Golden examples in fixtures.json are synthetic. Validate snapshot and event objects with schema.json and feed the sequences through each consumer's reducer. Additional fields or schema changes require an explicit version/provenance update; consumers reject unsupported major versions. Layout, animation, desk slots and credentials are not shared contract state.
