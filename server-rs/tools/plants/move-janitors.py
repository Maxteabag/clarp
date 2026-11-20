"""A Janitor is refused at the source snapshot and at the import route."""
import runner

RUNS = {"host": ["test", "--test", "host", "a_janitor_is_refused_before_any_move_and_its_neighbor_can_still_move"]}
ADMIN = "clarp-host/src/admin.rs"
EDGE = "clarp-host/src/edge.rs"
PLANTS = [
    (ADMIN, "host", "read a Janitor's conversation before refusing the move",
     "    movable(&agent)?;", '    if !only_files {let _ = get(&from, "/log", source_token);} movable(&agent)?;'),
    (ADMIN, "host", "allow a Janitor on a files-only move", "    movable(&agent)?;", "    if !only_files {movable(&agent)?;}"),
    (ADMIN, "host", "accept the Janitor flag", 'if agent["is_janitor"] == true', 'if agent["unused_flag"] == true'),
    (EDGE, "host", "ignore the incoming source row", "crate::admin::movable(&request.agent)", "crate::admin::movable(&Value::Null)"),
    (ADMIN, "host", "omit what to do with a custom Janitor", "or create a custom Janitor here from its definition.", "or skip it."),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
