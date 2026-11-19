#!/usr/bin/env python3
"""A user send's delivery in the kernel (feature send_delivery_state): the projection of a
receipt, the feed's pages, a page's rows, the query of GET /log and the hint's replay, each
wrong rule caught by the Python Host's own answers."""
import runner

K = "clarp-kernel/src/delivery.rs"
RUNS = {"vectors": ["test", "-p", "clarp-kernel", "--test", "delivery_vectors"]}
PLANTS = [(K, "vectors", why, before, after) for why, before, after in [
    ("a stored rev of zero counted", "value.as_u64().filter(|n| *n > 0).unwrap_or(0)", "value.as_u64().unwrap_or(0)"),
    ("a float taken for a whole number", "Value::Number(number) if !number.is_f64() => number", "Value::Number(number) => number"),
    ("the id given its prefix twice", 'if id.starts_with("u-") {', 'if false {'),
    ("an empty client id shown", '        .as_str()\n        .filter(|id| !id.is_empty())?;\n    let trace_id',
     '        .as_str()?;\n    let trace_id'),
    ("another agent's receipt shown", 'if rev == 0 || seq == 0 || field("agent_id").as_str() != Some(agent_id) {', 'if rev == 0 || seq == 0 {'),
    ("the row's origin not held", 'if origin.is_some_and(|origin| field("origin").as_str() != Some(origin)) {', 'if false {'),
    ("a sender of nothing taken as itself", "let said = if python::truthy(theirs) {", "let said = if !theirs.is_null() {"),
    ("handed over said of a pending one", 'Some("attempted") => HANDED_OVER,', 'Some("attempted" | "relaunched") => HANDED_OVER,'),
    ("a fenced one shown pending", 'Some("fenced") => STOPPED,', 'Some("fenced") => PENDING_LAUNCH,'),
    ("a Stop at the same generation stops it", "if i128::from(generation) > admitted {", "if i128::from(generation) >= admitted {"),
    ("a receipt without its generation shown", "let admitted = whole(field(\"user_stop_gen\"))?;", "let admitted = whole(field(\"user_stop_gen\")).unwrap_or(0);"),
    ("a row of another trace shown", 'let of_it = shown["for"]["trace_id"] == trace.as_str()\n            &&', 'let of_it = true\n            &&'),
    ("a row's empty origin not the user's", "let origin = if python::truthy(&turn[\"origin\"]) {", "let origin = if !turn[\"origin\"].is_null() {"),
    ("a cursor ahead of the Host served", "if since < 0 || since > i128::from(current) {", "if since < 0 {"),
    ("a first bootstrap page anywhere", "        if since != 0 {\n            return Page::expired();", "        if false {\n            return Page::expired();"),
    ("a cursor of another generation served", "if stop_generation != Some(i128::from(generation)) {", "if stop_generation.is_some_and(|g| g != i128::from(generation)) {"),
    ("until beyond the Host served", "let Some(until) = until.filter(|until| since <= *until && *until <= current) else {", "let Some(until) = until.filter(|until| since <= *until) else {"),
    ("a cursor behind the watermark served", "if since >= 1 && since < watermark {", "if since > 1 && since < watermark {"),
    ("a pointer of another seq trusted", "count(&receipt[\"seq\"]) == *seq\n", "true\n"),
    ("a full page said to be the last", "Some((seq, _, _)) if rows.len() == cap => (*seq, true),", "Some((seq, _, _)) if rows.len() > cap => (*seq, true),"),
    ("the end of a short page not the range's end", "_ => (high, false),", "_ => (rows.last().map_or(since, |row| row.0), false),"),
    ("until said on a normal page", "until: if bootstrap { until } else { None },", "until,"),
    ("a skipped pointer not counted", "            skipped += 1;\n", ""),
    ("a non-ASCII digit read", "if !(raw.is_ascii() && python::isdigit(raw)) {", "if !python::isdigit(raw) {"),
    ("an unstripped number refused", "let raw = python::strip(raw.unwrap_or(\"\"));\n    if raw.is_empty() {", "let raw = raw.unwrap_or(\"\");\n    if raw.is_empty() {"),
    ("bootstrap said by any truthy word", 'bootstrap: flag == "1" || flag == "true",', 'bootstrap: !flag.is_empty() && flag != "0",'),
    ("since=0 not a bootstrap", "let bootstrap = query.bootstrap || since == 0;", "let bootstrap = query.bootstrap;"),
    ("a cursor of another runtime served", "if continuation && query.carried != (scope.release.clone(), scope.instance.clone()) {", "if false {"),
    ("the first page held to a scope", "let continuation = !(bootstrap && since == 0 && query.until.is_none());", "let continuation = true;"),
    ("bootstrap fields on an expired page", "if bootstrap && !page.expired {", "if bootstrap {"),
    ("a hint with its state replayed", 'if stored["type"] != HINT {', 'if stored["type"] != "" {'),
    ("a hint of an empty identity replayed", ".all(|key| stored[*key].as_str().is_some_and(|text| !text.is_empty()));", ".all(|key| stored[*key].is_string());"),
    ("the runtime's mark given to a client", "if internal && python::truthy(&stored[mark]) {", "if python::truthy(&stored[mark]) {"),
]]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
