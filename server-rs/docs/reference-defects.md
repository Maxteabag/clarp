# Defects of the reference

What the conformance runs showed to be wrong, or unsteady, in the Python Host. The Rust Host does
not copy these. Each is reported to whoever owns the Python Host's releases; none is fixed from
here.

| # | What | Evidence | State |
|---|---|---|---|
| R1 | A message sent again under the same `client_msg_id`, after it was answered, leaves the conversation in one of two shapes: the reply is either the row written while the turn ran (with the turn's end on it), or the row imported from the tool's transcript (another id, no `turn`). Which one depends on timing | Conformance runs 43678950870 and 43679413710, scenario `retry-same-client-id`: the reference differs from itself | Scenario marked `unsteady`: compared, not counted. To report |
| R2 | `docs/protocol.md` section 4 says a plain send to a busy agent preempts the running turn unless `queue_if_busy` is set. For a Claude agent the Host queues it: `POST /send` answers `queued: true`, the running turn completes, the new message runs after | Run 43679413710, scenario `send-while-busy` | The Rust Host does what the Python Host does. The document is what is wrong. To report |
| R3 | Right after a turn ends, `GET /log` can show the reply twice, or show a state that changes a moment later: the Host is still importing the tool's transcript and folding it into the rows it wrote while the turn ran | Run 43678950870: two runs of the unchanged Host differed in 27 places until the driver waited for the conversation to stop changing | The driver waits three seconds of no change. A client has no such signal: it sees the intermediate states |
| R4 | After a Stop, messages that were already queued stayed held after `POST /turn-queue/resume`, and while held they counted as work in progress and suppressed the agent's goal wakes; `POST /turn-queue/<id>/send` answered 500 and removed the row | Observed on the live Host on 15 Nov 2026 (not by the suite); related issues 320, 249 | Report drafted, waits for the owner's word |
