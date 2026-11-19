#!/usr/bin/env python3
"""A user send's delivery on the Host (send_delivery_state): the agent's ledger of sends whose
delivery can still change, each change at its own revision of the agent's log; the actor's feed
since the Host started, read without the log; GET /log's fields and its 400; the hint at its two
places."""
import runner

A = "clarp-kernel/src/agent.rs"
D = "clarp-kernel/src/delivery.rs"
J = "clarp-kernel/src/journal.rs"
F = "clarp-actor/src/deliveries.rs"
L = "clarp-actor/src/lib.rs"
E = "clarp-host/src/edge.rs"
W = "clarp-host/src/wire.rs"
RUNS = {
    "ledger": ["test", "-p", "clarp-kernel", "--lib", "delivery::tests"],
    "feed": ["test", "-p", "clarp-actor", "--lib", "deliveries::tests"],
    "port": ["test", "-p", "clarp-actor", "--test", "actor", "the_delivery_of_ten_thousand_turns"],
    "host": ["test", "-p", "clarp-host", "--test", "host", "a_held_send_is_shown"],
}
PLANTS = [
    (A, "ledger", "a send left for later shown", "if origin.is_user() && matches!(admission, Admission::Queue | Admission::Direct) {", "if origin.is_user() {"),
    (A, "ledger", "an agent's send shown", "if origin.is_user() && matches!(admission, Admission::Queue | Admission::Direct) {", "if matches!(admission, Admission::Queue | Admission::Direct) {"),
    (A, "ledger", "the user's Stop not counted", "                    self.user_stop_generation += 1;\n", ""),
    (A, "ledger", "the user's Stop fences nothing", "                    self.deliveries.fence(self.at, *at_ms);\n", ""),
    (A, "ledger", "a send never handed over", "                    self.deliveries.hand(turn.0, revision, at_ms);\n", "                    let _ = (revision, at_ms);\n"),
    (D, "ledger", "a fenced send fenced again", ".filter(|send| !send.fenced);", ".filter(|_| true);"),
    (D, "ledger", "a change that does not count", "        self.rev += 1;\n", ""),
    (D, "host", "no Stop the user's", "pub fn is_users_stop(_by: &str) -> bool {\n    true", "pub fn is_users_stop(_by: &str) -> bool {\n    false"),
    (J, "host", "a batch's events at no position", "                    self.work\n                        .at(self.recorder.last_seq + self.items.len() as Seq);\n", ""),
    (J, "port", "what changed before the start told again", "        agent.deliveries_mut().folding(true);\n", ""),
    (F, "feed", "a fenced send not hinted", "let told = moved && send.fenced && !send.handed;", "let told = moved && send.handed;"),
    (L, "host", "a launch not hinted", "            if let Some(id) = self.deliveries.message_id(turn) {\n                told.push(id);", "            if let Some(id) = self.deliveries.message_id(turn) {\n                let _ = id;"),
    (A, "host", "a send behind a busy turn left pending", "                    if waits {\n", "                    if false {\n"),
    (A, "ledger", "a launch not told", "                    if launch {\n                        self.deliveries.launch(turn.0);", "                    if launch {\n                        let _ = turn;"),
    (F, "feed", "the feed at no position", "            head: self.latest,", "            head: 0,"),
    (L, "port", "a send's names read at every ask", "            let names = if self.deliveries.message_id(turn).is_some() {\n                None", "            let names = if false {\n                None"),
    (E, "host", "a bad number of the query let through", ".map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;\n    let scope", ".unwrap_or_default();\n    let scope"),
    (E, "host", "a scope of no release", "release: clarp_kernel::delivery::IN_PROCESS_RELEASE.to_string(),", "release: String::new(),"),
    (E, "host", "a page's rows not given theirs", "    answer[\"turns\"] = Value::Array(turns);\n", "    let _ = turns;\n"),
    (W, "host", "a hint without its message", "        \"message_id\": message_id,\n", ""),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
