#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What a turn of Antigravity or of Codex took, in tokens, as the Python Host counts it since its PR 413 (R48):
Antigravity's reader of a turn's steps and result, the share of a turn in Codex's running total, and the actor
that keeps it.
"""
import runner

RUNS = {
    'agy': ['test', '-p', 'clarp-backend', '--test', 'agy_usage_vectors'],
    'codex': ['test', '-p', 'clarp-kernel', '--test', 'codex_turn_usage_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_turn_of_codex_is_counted_by_what_its_thread_grew_by'],
}
A = 'clarp-backend/src/agy.rs'
U = 'clarp-kernel/src/usage.rs'
C = 'clarp-actor/src/lib.rs'
# NOT PLANTED, each with its reason:
#   agy.rs: that the first line and the result are told once (`"init" | "result"`): nothing of the first line is
#     counted, and nothing after the result is read.
#   agy.rs: that a tool's step is told once (`update["step_type"] == "tool"`): a step says what it took only when it
#     is DONE, which is told once already.
PLANTS = [
    # ---- a line told again
    (A, 'agy', 'a line told again word for word is taken again', '        if once && !self.seen.insert(', '        if false && !self.seen.insert('),
    (A, 'agy', 'a done in small letters is told once too', '            || update["state"] == "DONE"', '            || text_of(&update["state"]).eq_ignore_ascii_case("DONE")'),
    # ---- a step
    (A, 'agy', 'a step that is not done says what it took', '        let done = text_of(&update["state"]).eq_ignore_ascii_case("DONE");', '        let done = true;'),
    (A, 'agy', 'a done in small letters is none', '        let done = text_of(&update["state"]).eq_ignore_ascii_case("DONE");', '        let done = update["state"] == "DONE";'),
    (A, 'agy', 'a step is counted however often it is told', '            if usable && key.is_none_or(|key| self.counted.insert(key)) {', '            if usable {'),
    (A, 'agy', 'steps without an index are one step', '                Value::Null => None,', '                Value::Null => Some(String::new()),'),
    (A, 'agy', 'an index written as a text is another step', '                Value::String(index) => Some(index.clone()),', '                Value::String(index) => Some(format!("{index:?}")),'),
    (A, 'agy', 'a fraction is the step of its whole number', '                index => Some(clarp_kernel::python::repr(index)),', '                index => Some(clarp_kernel::python::repr(index).trim_end_matches(".0").to_string()),'),
    (A, 'agy', 'a step that says only its total said what it took', '            let usable = figures.iter().any(|(name, _)| *name != TOTAL);', '            let usable = !figures.is_empty();'),
    (A, 'agy', 'a step that says it took nothing said nothing', '            let usable = figures.iter().any(|(name, _)| *name != TOTAL);', '            let usable = figures.iter().any(|(name, figure)| *name != TOTAL && *figure != 0);'),
    # ---- the figures
    (A, 'agy', 'a figure written as a text is none', '            Value::String(text) => clarp_kernel::python::int(text),', '            Value::String(_) => None,'),
    (A, 'agy', 'a truth is a figure', '            Value::String(text) => clarp_kernel::python::int(text),', '            Value::String(text) => clarp_kernel::python::int(text),\n            Value::Bool(truth) => Some(i128::from(*truth)),'),
    (A, 'agy', 'a fraction is no figure', '                .or_else(|| number.as_f64().filter(|value| value.is_finite()).map(cut)),', '                .or_else(|| number.as_f64().filter(|value| value.is_finite()).and(None)),'),
    (A, 'agy', 'a fraction is rounded', '    value.trunc() as i128', '    value.round() as i128'),
    (A, 'agy', 'what was read from the cache is not read', '        ("cache_read_tokens", "cache_read_input_tokens"),', '        ("cache_reads", "cache_read_input_tokens"),'),
    (A, 'agy', 'what the model thought is not read', '        ("thinking_tokens", "thinking_tokens"),', '        ("thoughts", "thinking_tokens"),'),
    (A, 'agy', 'what was read from the cache is not added up', '                "cache_read_input_tokens" => self.cache_read += figure,\n', ''),
    (A, 'agy', 'what was read from the cache did not go in', '            whole(self.input + self.cache_read),', '            whole(self.input),'),
    (A, 'agy', 'what the model thought is not what it wrote', '            whole(self.output + self.thinking),', '            whole(self.output),'),
    # ---- the turn
    (A, 'agy', "the result's figure counts though the steps said what they took", '        let took = if self.took.any {', '        let took = if false {'),
    (A, 'agy', "the result's figure never counts", '            (!terminal.is_empty()).then_some(took)', '            None'),
    (A, 'agy', 'a result that says no figure took nothing', '            (!terminal.is_empty()).then_some(took)', '            Some(took)'),
    # ---- Codex: the share of a turn in its thread's total
    (U, 'codex', 'where a turn began is taken to be nothing where it is not known', '    let (began_in, began_out) = began_at?;', '    let (began_in, began_out) = began_at.unwrap_or((0, 0));'),
    (U, 'codex', 'what went in and fell is counted as nothing', '        total.0.checked_sub(began_in)?,', '        total.0.saturating_sub(began_in),'),
    (U, 'codex', 'what came out and fell is counted as nothing', '        total.1.checked_sub(began_out)?,', '        total.1.saturating_sub(began_out),'),
    (U, 'codex', 'what went in is measured from where what came out began', '        total.0.checked_sub(began_in)?,', '        total.0.checked_sub(began_out)?,'),
    (C, 'host', "a turn of Codex took its thread's total", '        let share = clarp_kernel::usage::share(facts.base, (tokens_in, tokens_out));', '        let share = Some((tokens_in, tokens_out));'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
