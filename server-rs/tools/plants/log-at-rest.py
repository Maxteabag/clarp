"""The port releases cached pages only after an idle opening or a turn's final commit."""
import runner

ACTOR = "clarp-actor/src/lib.rs"
HOST = "clarp-host/src/agent.rs"
PORT = "clarp-ports/src/log.rs"
SQLITE = "clarp-store-sqlite/src/lib.rs"
RUNS = {
    "actor": ["test", "-p", "clarp-actor", "--test", "actor", "log_at_rest"],
    "worker": ["test", "-p", "clarp-host", "--lib", "log_at_rest_after_the_workers_last_opening_read"],
    "sqlite": ["test", "-p", "clarp-store-sqlite", "--test", "conformance", "log_at_rest"],
}
PLANTS = [
    (PORT, "actor", "require engines to implement cache release to use a log",
     "fn at_rest(&self) {}", 'fn at_rest(&self) { panic!("engine needs a cache"); }'),
    (SQLITE, "sqlite", "leave the cached pages in place",
     'self.conn.execute_batch("PRAGMA shrink_memory")', 'self.conn.execute_batch("SELECT 1")'),
    (SQLITE, "sqlite", "cap the cache for future turns as well",
     '"PRAGMA shrink_memory"', '"PRAGMA cache_size=-32; PRAGMA shrink_memory"'),
    (ACTOR, "actor", "leave a standalone recovered log hot", "        actor.finish_opening();",
     "        let _ = &mut actor;"),
    (ACTOR, "actor", "finish an opening more than once", "std::mem::take(&mut self.opening)",
     "{ self.opening = false; true }"),
    (ACTOR, "actor", "trim when a live turn rejoins or the next turn owns the log",
     "!self.opening && self.recorder.agent().live().is_none()", "!self.opening"),
    (ACTOR, "actor", "trim while a terminal record is still being folded during recovery",
     "!self.opening && self.recorder.agent().live().is_none()", "self.recorder.agent().live().is_none()"),
    (ACTOR, "actor", "consider a parked turn idle",
     "self.recorder.agent().live().is_none() {\n            self.recorder.log().at_rest()",
     "!self.recorder.agent().live().is_some_and(Turn::has_process) {\n            self.recorder.log().at_rest()"),
    (ACTOR, "actor", "retain the cache when the last running turn ends",
     "            self.rest_if_idle();\n            for attempt", "            for attempt"),
    (ACTOR, "actor", "retain the cache after stopping a turn parked between attempts",
     "if stopped_between_attempts {", "if stopped_between_attempts && self.recorder.agent().live().is_some() {"),
    (ACTOR, "actor", "trim on every stop of an already idle mailbox",
     "if stopped_between_attempts {", "if stopped_between_attempts || self.recorder.agent().live().is_none() {"),
    (ACTOR, "actor", "trim on every spool notification even if nothing ended",
     "pub fn spool_changed(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {",
     "pub fn spool_changed(&mut self, now_ms: i64) -> Result<Outputs, ActorError> { self.recorder.log().at_rest();"),
    (HOST, "worker", "omit cache release when the worker finishes its projections",
     "            actor.finish_opening();", "            let _ = actor;"),
    (HOST, "worker", "release before the worker reads its projections",
     "Actor::open_for_recovery(", "Actor::open("),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
