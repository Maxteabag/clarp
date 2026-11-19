"""Wrong rules for what is kept of a failed routing CLI's words (the Python Host's PR 445, R85)."""
import runner

K='clarp-kernel/src/failures.rs'
O='clarp-host/src/once.rs'
RUNS={
    'vectors':['test','--test','routing_failure_vectors'],
    'once':['test','-p','clarp-host','--lib','a_tool_is_run_once_with_its_question'],
    'router':['test','--test','host','a_routing_tool_that_fails_is_told_by_its_facts'],
    'keeper':['test','--test','host','the_goal_keeper_keeps_a_goal_current_beside_its_owner'],
}
PLANTS=[(K,'vectors',label,old,new) for label,old,new in [
    ('a run of 23','pub const ECHO_RUN: usize = 24;','pub const ECHO_RUN: usize = 23;'),
    ('a run of 25','pub const ECHO_RUN: usize = 24;','pub const ECHO_RUN: usize = 25;'),
    ('a line of 299','pub const KEPT_MOST: usize = 300;','pub const KEPT_MOST: usize = 299;'),
    ('control characters kept',"crate::python::blank(sign) || sign <= '\\u{1f}' || sign == '\\u{7f}'","crate::python::blank(sign)"),
    ('a bytes repr read as code points','python_strip(&plain(&nfkc(&unescaped(text, true)))),','python_strip(&plain(&nfkc(&unescaped(text, false)))),'),
    ('escapes not undone','python_strip(&plain(&nfkc(&unescaped(text, false)))),\n        python_strip(&plain(&nfkc(&unescaped(text, true)))),','python_strip(&plain(&nfkc(text))),\n        python_strip(&plain(&nfkc(text))),'),
    ('the last part not normalised','said.extend(run.nfkc());\n    said\n','said.extend(run.chars());\n    said\n'),
    ('what the reference has not assigned normalised','if unassigned_in_reference(sign) {','if false {'),
    ('bytes read in tens','u8::from_str_radix(pair, 16)','u8::from_str_radix(pair, 10)'),
    ('a short prompt asks for a whole run','let run = ECHO_RUN.min(request.chars().count());','let run = ECHO_RUN;'),
    ('nothing to compare echoes','if request.is_empty() {\n        return false;','if false {\n        return false;'),
    ('the line not cut','.take(KEPT_MOST)','.take(usize::MAX)'),
    ('an echo kept','if line.is_empty() || echoes(&line, prompt) {','if line.is_empty() {'),
    ('a category of no failure kept','if category != INTERRUPTED && !PROVIDER_FAILURES.contains(&category) {','if false {'),
    ('a refusal read from the line only','let denied = DENIED.is_match(words);','let denied = false;'),
    ('an interruption moved on from','let moves_on = category != INTERRUPTED\n        && !denied','let moves_on = true\n        && !denied'),
    ('the line without its category','Some(status) => format!("{backend} exited with status {status} ({category})"),','Some(status) => format!("{backend} exited with status {status}"),'),
    ('no status, other words','None => format!("{backend} failed ({category})"),','None => format!("{backend} exited ({category})"),'),
    ('a timeout not named','if failure.category == TIMEOUT {','if false {'),
    ('an exit not named','return Some(format!("{}_exit_{status}", failure.backend));','return Some(format!("{}_failed", failure.backend));'),
    ('agy\'s prefix not read','AGY.captures(&failure.line)','AGY.captures("")'),
]]
for file,run,label,old,new in [
    (O,'once','what it printed before what it complained of','let said = if complained.is_empty() {','let said = if !complained.is_empty() {'),
    (O,'once','a signal as a status of its own','.or_else(|| done.status.signal().map(|signal| -i64::from(signal)));','.or_else(|| done.status.signal().map(i64::from));'),
    (O,'once','held against no question','let failed = clarp_kernel::failures::cli_failure(backend, said, prompt, status);','let failed = clarp_kernel::failures::cli_failure(backend, said, "", status);'),
    (O,'router',"the router's question not given",'run(&launch, cwd, within, (backend, &question.prompt)).await;','run(&launch, cwd, within, (backend, "")).await;'),
    ('clarp-host/src/model_fallbacks.rs','router','moved on by the words','failure.retry = facts.moves_on;','failure.retry = retryable(&failure.detail);'),
    ('clarp-host/src/keeper.rs','keeper',"the keeper's code not from the facts",'named.map_or(Cow::Borrowed(error.code), Cow::Owned)','named.map_or(Cow::Borrowed(error.code), |_| Cow::Borrowed(error.code))'),
]:
    PLANTS.append((file,run,label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
