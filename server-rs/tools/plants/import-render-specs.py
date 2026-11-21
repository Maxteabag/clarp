"""An unavailable render spec stays at its source, reported by name and count."""
import runner

ADMIN = "clarp-host/src/admin.rs"
TEST = "a_move_names_every_render_spec_left_on_the_source_and_takes_its_other_artifacts"
RUNS = {"move": ["test", "-p", "clarp-host", "--test", "host", TEST]}
PLANTS = [
    (ADMIN, "move", "refuse an agent whose library includes a render spec",
     "left.push(line);", 'return Err(format!("unsupported artifact type: {line}"));'),
    (ADMIN, "move", "keep a metadata-only placeholder",
     "left.push(line);", '''let mut placeholder = artifact.clone(); placeholder["type"] = json!("document");
                library.push(json!({"artifact":placeholder})); left.push(line);'''),
    (ADMIN, "move", "skip an unrecognized library type too",
     'artifact["type"] != "render_spec"', 'artifact["type"] != "render_spec" && artifact["type"] != "unknown"'),
    (ADMIN, "move", "lose the id of a skipped spec",
     'let id = artifact["artifact_id"].as_str().unwrap_or("");', 'let id = artifact["session"].as_str().unwrap_or("");'),
    (ADMIN, "move", "lose the title of a skipped spec",
     'let title = artifact["title"].as_str().unwrap_or("");', 'let title = artifact["summary"].as_str().unwrap_or("");'),
    (ADMIN, "move", "let a title with a newline run over into the next report line",
     '{id:?} — {title:?}', '{id:?} — "{title}"'.replace('"', '\\"')),
    (ADMIN, "move", "forget the first skipped spec in the count",
     'let count = left.len();', 'let count = left.len().saturating_sub(1);'),
    (ADMIN, "move", "omit the report when exactly one spec was left",
     'if left.is_empty() {', 'if left.len() <= 1 {'),
    (ADMIN, "move", "print a skipped section with an empty list",
     'if left.is_empty() {', 'if false {'),
    (ADMIN, "move", "print the plural for one omitted spec",
     'let kind = if count == 1', 'let kind = if count == 0'),
    (ADMIN, "move", "combine multiple omitted specs on one line",
     'left.join("\\n")', 'left.join(", ")'),
    (ADMIN, "move", "page using imported rows only",
     'offset += count;', 'offset += rows.iter().filter(|row| row["type"] != "render_spec").count();'),
    (ADMIN, "move", "count the skipped specs as imported library items",
     'let made = match library.len()', 'let made = match library.len() + left.len()'),
    (ADMIN, "move", "omit the skipped section on an agent move",
     '{skipped}{more}{heard}', '{more}{heard}'),
    (ADMIN, "move", "omit the skipped section on a file retry",
     '{came} here.\\n{skipped}{more}', '{came} here.\\n{more}'),
    (ADMIN, "move", "ask for files named only by skipped rows",
     'let wants = wanted(&gallery, &library, &plans, &turns);',
     'let mut mentions = library.clone(); mentions.extend(left.iter().map(|line|json!(line))); let wants = wanted(&gallery, &mentions, &plans, &turns);'),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
