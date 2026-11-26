#!/usr/bin/env python3
"""Changes to the shared OpenCode and DeepSeek command and catalogue rules."""
import runner

KERNEL = 'clarp-kernel/src/opencode.rs'
ARGS = "clarp-tool-args/src/opencode.rs"
RUNS = {'vectors': ['test', '--test', 'backend_opencode_vectors']}
PLANTS = [
    (ARGS, 'vectors', 'thinking is enabled for one-shot questions too', 'if thinking {', 'if !thinking {'),
    (ARGS, 'vectors', 'the effort becomes a model option', '("--variant", effort)', '("--model", effort)'),
    (ARGS, 'vectors', 'a nested model id loses its provider', "model.split_once('/')?", "model.rsplit_once('/')?"),
    (KERNEL, 'vectors', 'an explicit question permission is discarded', 'rules.entry("question").or_insert(json!("deny"));', 'rules.insert("question".into(), json!("deny"));'),
    (KERNEL, 'vectors', 'system text enters the answer', 'Some("user" | "system")', 'Some("user")'),
    (KERNEL, 'vectors', 'an uppercase family name falls out of the catalogue', 'id.to_lowercase().contains("deepseek")', 'id.contains("deepseek")'),
    (KERNEL, 'vectors', 'model labels use the raw hosting provider', '"fireworks-ai" => "Fireworks"', '"fireworks-ai" => "fireworks-ai"'),
]

RUNS.update(streams=['test', '--test', 'opencode_stream'], history=['test', '--test', 'opencode_history'],
            stop=['test', '--test', 'shim', '--', 'a_direct_child_can_abort_before_its_group_is_terminated'],
            # Cargo must rebuild both binaries: each served turn starts clarp-shim.
            host=['test', '--test', 'host', '--test', 'shim', '--', 'opencode_and_deepseek_run_resume_abort_and_report_their_native_sessions'])

def rules(file, run, changes):
    for name, old, new in changes:
        PLANTS.append((file, run, name, old, new))

rules(ARGS, 'vectors', [
    ('does not request automatic approval', '"--auto"', '"--ask"'),
    ('a new turn has a session argument', 'if !value.is_empty()', 'if value.is_empty()'),
    ('the provider is used as the model', '"modelID": model', '"modelID": provider'),
    ('a model without a provider is accepted', '!provider.is_empty() && !model.is_empty()', '!model.is_empty()'),
    ('a provider without a model is accepted', '!provider.is_empty() && !model.is_empty()', '!provider.is_empty()'),
])
rules(KERNEL, 'vectors', [
    ('the primary session id is ignored', '["sessionID", "session_id", "sessionId"]', '["missing", "session_id", "sessionId"]'),
    ('session id whitespace is kept', 'event[k].as_str().map(python::strip)', 'event[k].as_str()'),
    ('nested session properties are missed', '["properties", "session", "data", "part"]', '["other", "session", "data", "part"]'),
    ('nested part text is lost', 'return text(&value["part"]);', 'return String::new();'),
    ('list items run together', r'.join("\n")', '.join("")'),
    ('alternative event spelling is missed', '&event["event"]', '&event["type"]'),
    ('model ids with a slash accept duplicates', '!seen.insert(id)', 'false && !seen.insert(id)'),
    ('verbose variants are ignored', 'v.contains_key(*e)', 'true || v.contains_key(*e)'),
    ('detail-free variants disappear', 'variants.is_none_or', 'variants.is_some_and'),
    ('unknown model is labeled by a fixed provider', 'other => other,', 'other => if other.is_empty() { other } else { "Other" },'),
    ('a verbose display name is ignored', 'label.to_string()', 'id.to_string()'),
    ('remembered plain ids are discarded', "!id.contains('/')", "id.contains('/')"),
])
rules('clarp-shim/src/main.rs','stop',[
    ('the child is never asked', 'let _ = kill(pid, early.signal);', 'let _ = (pid,early.signal);'),
    ('the entire group is asked', 'let _ = kill(pid, early.signal);', 'let _ = killpg(pid,early.signal);'),
    ('the wait exceeds its bound', 'stage, early.wait,', 'stage, early.wait * 3,'),
    ('the group is signalled immediately', 'stage, early.wait,', 'stage, Duration::ZERO,'),
    ('a second stop asks again', '!matches!(*stage, StopStage::Ready)', 'matches!(*stage, StopStage::Signalled)'),
    ('the driver exit does not end the wait', '                    !self.leader_exited.load(Ordering::SeqCst)\n', '                    true\n'),
])
rules('clarp-shim/src/opencode.rs','host',[
    ('the server binds to an unusable loopback address', '"--hostname", "127.0.0.1"', '"--hostname", "127.0.0.2"'),
    ('prompts go to another path', '{session}/prompt_async', '{session}/bad_prompt'),
    ('the abort goes to another path', '{path}/abort', '{path}/stop'),
    ('native session names are omitted', '"sessionID":sid', '"missing":sid'),
    ('the fallback resumes no session', 'req["session"].as_str().unwrap_or("")', '""'),
])
PLANTS.append(('clarp-shim/src/opencode_http.rs', 'host', 'a resumed session id can change', 'result["id"].as_str() != Some(old)', 'result["id"].as_str().is_none()'))
PLANTS.append(('clarp-shim/src/opencode_follow.rs', 'host', 'the wrong permission is submitted', '"reply":"once"', '"reply":"always"'))
PLANTS.append(('clarp-shim/src/opencode_follow.rs', 'host', 'the requested effort is lost', 'body["variant"] = json!(effort)', 'body["variant"] = Value::Null'))
PLANTS.append(('clarp-shim/src/opencode_http.rs', 'host', 'the working directory is sent under another key', '{joined}directory={}', '{joined}cwd={}'))
rules('clarp-backend/src/history/opencode.rs','history',[
    ('successful tools remain running', '_ => "ok",', '_ => "running",'),
    ('tool failures are successful', '"error" => "error",', '"error" => "ok",'),
    ('a pending call is considered completed', '"running" | "pending" => "running"', '"running" => "running"'),
    ('search is shown as list', '"grep" => "Search"', '"grep" => "List"'),
    ('a reading group loses its target', 'format!("{pattern} in {path}")','pattern.to_string()'),
    ('completed file edits are called writes', '"Edited"', '"Wrote"'),
    ('added file lines are old diff lines', '"diff_new"', '"diff_old"'),
    ('an error of a call is not marked', 'error.strip_prefix("Error: ").unwrap_or(error)', 'error'),
    ('todo updates appear as commands', '"Updated plan"', '"Ran"'),
    ('a skill hides its name', '"Loaded skill"', '"Skill"'),
    ('a task loses its prompt', '("prompt", "Task", "detail", 300)', '("other", "Task", "detail", 300)'),
    ('a task reply is hidden', '"Result", "status"', '"Output", "status"'),
    ('a question loses its options', 'cells::line(t, "Option", "muted")','cells::line(t, "Value", "muted")'),
    ('empty assistant messages show aborts', 'e["name"] != "MessageAbortedError"', 'true'),
    ('text blocks are run together', r'parts.join("\n\n")','parts.join("")'),
    ('the charge time is interpreted as seconds', '*created > 10_000_000_000', '*created > i64::MAX-1'),
    ('a stale running call stays running', 'json!("recorded")','json!("running")'),
    ('an assistant error is treated as a retryable reply', 'row["failed"] = json!(true)', 'row["failed"] = json!(false)'),
])

rules('clarp-backend/src/opencode.rs','streams',[
    ('a command is named read', '"bash" => "Bash"', '"bash" => "Read"'),
    ('a file read has its raw tool name', '"read" => "Read"', '"read" => "read"'),
    ('the old text argument is lost', '"oldString" => "old_string"', '"oldString" => "old"'),
    ('the new text argument is lost', '"newString" => "new_string"', '"newString" => "new"'),
    ('file paths retain their wire spelling', '"filePath" => "file_path"', '"filePath" => "filePath"'),
    ('replacement mode is lost', '"replaceAll" => "replace_all"', '"replaceAll" => "all"'),
    ('a long input is clipped a character early', 'clip(s, 400)', 'clip(s, 399)'),
    ('a long input is clipped a character late', 'clip(s, 400)', 'clip(s, 401)'),
    ('a list loses its fiftieth entry', '.take(50)', '.take(49)'),
    ('a list keeps a fifty-first entry', '.take(50)', '.take(51)'),
    ('text inside a list is clipped a character early', 'clip(s, 200)', 'clip(s, 199)'),
    ('text inside a list is clipped a character late', 'clip(s, 200)', 'clip(s, 201)'),
    ('the status of a returned call is reversed', 'let failed = status == "error"', 'let failed = status != "error"'),
    ('a tool that has completed stays running', 'if matches!(status, "completed" | "error") && call.status', 'if matches!(status, "error") && call.status'),
    ('a provider failure loses its own message', '&e["data"]["message"]', '&e["nothing"]["message"]'),
    ('an unnamed failure loses its default', 'unwrap_or("opencode error")', 'unwrap_or("failed")'),
    ('what a step took in counts as output', 'tokens["input"].as_f64()', 'tokens["output"].as_f64()'),
    ('what a step wrote counts as input', 'tokens["output"].as_f64()', 'tokens["input"].as_f64()'),
    ('steps are charged twice', '*self.cost.get_or_insert(0.0) += cost', '*self.cost.get_or_insert(0.0) += cost * 2.0'),
    ('a clean exit is taken for failure', '*code != Some(0)', '*code == Some(0)'),
    ('words inside a step are run together', '.join("\\n\\n")', '.join("")'),
])
HISTORY='clarp-backend/src/history/opencode.rs'
PLANTS.append((HISTORY, "history", 'history rule changed: .take(40)', '.take(40)', '.take(39)'))
PLANTS.append((HISTORY, "history", 'history rule changed: .take(40)', '.take(40)', '.take(41)'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Agent failed"', '"Agent failed"', '"Failed agent"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Agent finished"', '"Agent finished"', '"Ended agent"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Search"', '"Search"', '"Looking"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Session", "muted"', '"Session", "muted"', '"Session", "status"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Type", "muted"', '"Type", "muted"', '"Type", "detail"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Answer", "status"', '"Answer", "status"', '"Answer", "muted"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "Option", "muted"', '"Option", "muted"', '"Option", "detail"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "tool-calls"', '"tool-calls"', '"tool_call"'))
PLANTS.append((HISTORY, "history", 'history rule changed: "final_answer"', '"final_answer"', '"final"'))
PLANTS.append((HISTORY, "history", 'history rule changed: if Some(i) != last', 'if Some(i) != last', 'if Some(i) == last'))
PLANTS.append((HISTORY, "history", 'history rule changed: role == "assistant" && !failed', 'role == "assistant" && !failed', 'role == "assistant" && failed'))
RUNS['port']=['test','-p','clarp-shim','--bin','clarp-shim','announcements_are_loopback_only_bounded_and_complete']
DRIVER='clarp-shim/src/opencode.rs'
# The server's HTTP side and a turn's events, which the per-turn driver and the lane share (issue 763).
HTTP='clarp-shim/src/opencode_http.rs'
FOLLOW='clarp-shim/src/opencode_follow.rs'
rules(HTTP,'port',[
    ('a zero TCP port is used', '.filter(|n| *n != 0)', '.filter(|_| true)'),
    ('a port parsed at end of file is lost','chain((n == 0).then_some', 'chain((n != 0).then_some'),
    ('an oversized token is accepted','&& token.len() <= 8192','&& true'),
    ('a valid port following startup noise is lost','(_, "listening") => 1','(_, "listening") => 0'),
])
rules('clarp-host/src/logins/opencode.rs','host',[
    ('signed-in harness reports no credential','row["credential_present"] = json!(true)', 'row["credential_present"] = json!(false)'),
    ('DeepSeek accepts every signed-in provider','if deepseek','if !deepseek'),
    ('the signed-in account name is dropped','row["account"] = json!(account)','row["account"] = json!("")'),
    ('the sign-in evidence is called signed out','row["state"] = json!("valid")','row["state"] = json!("signed_out")'),
])
rules(HISTORY,'host',[
    ('subagents are offered as native conversations',' AND parent_id IS NULL',' AND parent_id IS NOT NULL'),
    ('archived conversations are offered','time_archived IS NULL','time_archived IS NOT NULL'),
    ('native time in seconds is divided again','mtime/=1000','mtime/=100'),
    ('session id lookup uses another column','SELECT 1 FROM session WHERE id=?','SELECT 1 FROM session WHERE title=?'),
    ('a native session loses its stored parts','SELECT message_id,data FROM part','SELECT id,data FROM part'),
])
PLANTS.append(('clarp-shim/src/main.rs', 'stop', 'an uncooperative descendant survives an early driver exit',
               'Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);',
               'if !self.leader_exited.load(Ordering::SeqCst) { Self::signal(self.pid, &self.to_spool, Signal::SIGKILL); }'))
rules(HTTP, 'host', [
    ('a new session has no permissions set', '"permission":rules', '"ignored":rules'),
    ('a resumed session is created afresh', 'if old.is_empty()', 'if !old.is_empty()'),
])
rules(FOLLOW, 'host', [
    ('child permission prompts stay unanswered', 'self.sessions.insert(id.to_string());', 'self.sessions.remove(id);'),
    ('a question is sent to the permission route', 'if kind == "permission.asked"', 'if kind == "question.asked"'),
    ('a permission of another session is approved', 'if props["sessionID"]\n                    .as_str()\n                    .is_some_and(|id| self.sessions.contains(id))',
        'if props["sessionID"].is_string()'),
    ('an idle before the first step ends the turn', '"session.idle" if mine && self.busy => return Ok(true)', '"session.idle" if mine => return Ok(true)'),
    ('main session events are mistaken for foreign events', 'let mine = props["sessionID"].as_str() == Some(&self.sid)', 'let mine = props["sessionID"].as_str() != Some(&self.sid)'),
    ('the model pair goes under another key', 'body["model"] = model', 'body["other"] = model'),
    ('the prompt is sent as a non-text part', '"parts":[{"type":"text","text":text}]', '"parts":[{"type":"other","text":text}]'),
])
rules('clarp-host/src/logins/opencode.rs','host',[
    ('a picker model is grouped under another key','model["model_provider"] = json!(provider)','model["provider"] = json!(provider)'),
    ('the picker does not offer provider grouping','row["harness_providers"] = json!(true)','row["harness_providers"] = json!(false)'),
])
rules(DRIVER, 'host', [
    ('normal shutdown kills the server without asking it to end', 'kill(server, Signal::SIGTERM)', 'kill(server, Signal::SIGKILL)'),
    ('a server is killed before its five seconds are up', 'wait.recv_timeout(Duration::from_secs(5))', 'wait.recv_timeout(Duration::from_secs(1))'),
    ('an externally aborted driver reports success', 'let _ = kill(Pid::this(), Signal::SIGTERM);', 'std::process::exit(0);'),
])
MAIN = 'clarp-shim/src/main.rs'
WAIT_FOR_STOP = """    stage = control.changed.wait_while(stage, |s| matches!(s, StopStage::Asked)).unwrap_or_else(PoisonError::into_inner);"""
rules(MAIN,'stop',[
    ('the turn end waits for group escalation', '    if to_spool.send(ToSpool::Last(entry, done)).is_ok() {',
     WAIT_FOR_STOP + '\n    if to_spool.send(ToSpool::Last(entry, done)).is_ok() {'),
    ('group escalation can append to a finished spool', 'to_spool.send(ToSpool::Last(entry, done))', 'to_spool.send(ToSpool::Now(entry, done))'),
])
RUNS['spool_end'] = ['test','-p','clarp-shim','--bin','clarp-shim','the_last_spool_record_discards']
rules(MAIN,'spool_end',[
    ('a queued kill is written after the final record', 'let last = matches!(&message, ToSpool::Last(..));', 'let last = false;'),
])
# The OpenCode lane: one kept serve per agent (issue 763).
RUNS.update(lane_digest=['test', '-p', 'clarp-host', '--lib', 'lane::opencode'],
            lane_lib=['test', '-p', 'clarp-shim', '--lib', 'opencode_lane'],
            lane_host=['test', '--test', 'host', '--test', 'shim', '--', 'opencode_chat_lane'])
LANE_HOST = 'clarp-host/src/lane/opencode.rs'
LANE_SHIM = 'clarp-shim/src/opencode_lane_runtime.rs'
PLANTS.append((LANE_HOST, 'lane_digest', 'a moved pin keeps the old lane',
               'put("program-content", &program_identity(&program));', ''))
PLANTS.append(('clarp-shim/src/opencode_lane.rs', 'lane_lib', 'a 2.x server is kept',
               "version.split('.').next() == Some(\"1\")", 'true'))
PLANTS.append((LANE_HOST, 'lane_host', 'a lane started otherwise takes the turn',
               '            && state.options == digest\n', ''))
PLANTS.append((LANE_SHIM, 'lane_host', 'a stopped lane turn reads as completed',
               'Ok(true) if stopping => self.end(None, Some(nix::libc::SIGINT), None),',
               'Ok(true) if stopping => self.end(Some(0), None, None),'))
# Steered words on the OpenCode lane (issue 784).
rules(LANE_SHIM, 'lane_host', [
    ('a lane turn never says it takes steered words', 'turn.spool.steer_open(now)?;', ''),
    ('steered words are read by an answer to the user message before them', 'info["role"] == "assistant"', 'info["role"] == "user"'),
    ('steered words go under a message OpenCode names', 'posted["messageID"] = json!(message);', ''),
    ('unread steered words stay in the session', 'match self.serve.client.delete(&path, 10) {', 'match crate::opencode_http::Result::<i32>::Ok(500) {'),
    ('words not taken back run again', 'turn.spool.steer_read(&id, super::now_ms())?;', 'let _ = &id;'),
    ('a stale idle ends the turn under words OpenCode runs again', 'if !stopping && self.session_runs() == Some(true) {', 'if false {'),
    ('words OpenCode already answered are taken back at a stale idle', 'if !stopping && by_order && self.answered(&unread) {', 'if false {'),
])
PLANTS.append(('clarp-backend/src/opencode.rs', 'lane_host', "a step's words wait for the next step, after words read between them",
               '                self.usage(p);\n                self.flush(out);\n', '                self.usage(p);\n'))
# Steers OpenCode reads together (issue 862).
RUNS['lane_shim'] = ['test', '-p', 'clarp-shim', '--bin', 'clarp-shim', 'opencode_lane_runtime']
PLANTS.append((LANE_SHIM, 'lane_host', 'only the message an answer names is read, not those stored before it',
               '    parent.starts_with("msg_") && message <= parent', '    parent.starts_with("msg_") && message == parent'))
PLANTS.append((LANE_SHIM, 'lane_shim', 'a message stored after an answer\'s parent counts as answered',
               '    parent.starts_with("msg_") && message <= parent', '    parent.starts_with("msg_")'))
# A turn held open by message order alone is rechecked (issue 864).
PLANTS.append((LANE_SHIM, 'lane_host', 'a turn held open by message order alone waits for a run that never comes',
               '                    self.recheck = Some((Instant::now() + RECHECK, false));\n', ''))
PLANTS.append((LANE_SHIM, 'lane_host', 'at the recheck an answer to a later own steer proves nothing',
               '    posted[own..]\n', '    posted[own..=own]\n'))
PLANTS.append((LANE_SHIM, 'lane_host', 'a late loop rechecks before it takes the busy event waiting for it',
               '    fn next_input(&mut self) -> io::Result<Option<Input>> {\n        loop {\n',
               '    fn next_input(&mut self) -> io::Result<Option<Input>> {\n        loop {\n            if let Some((at, failed)) = self.recheck\n                && Instant::now() >= at\n            {\n                self.recheck = None;\n                self.look_again(failed);\n                continue;\n            }\n'))
PLANTS.append((LANE_SHIM, 'lane_host', 'a failed read at the recheck ends the turn at once',
               '            None if !failed && !unread.is_empty() => {', '            None if false => {'))
PLANTS.append((LANE_SHIM, 'lane_host', 'at the recheck the stored list is not asked',
               '        match self.stored() {\n            Some(list) => {', '        match None::<Value> {\n            Some(list) => {'))
# What OpenCode writes into its configuration folders itself is no part of a lane's digest (issue 864's warm lane).
PLANTS.append((LANE_HOST, 'lane_digest', "OpenCode's own plugin in a package file moves the digest",
               '    dependencies.remove(OPENCODE_PLUGIN);\n', ''))
PLANTS.append((LANE_HOST, 'lane_digest', "a plugin someone adds to a package file moves nothing",
               '        Value::Object(dependencies).to_string().into_bytes()', '        Vec::new()'))
PLANTS.append((LANE_HOST, 'lane_digest', "OpenCode's default configuration moves the digest",
               'if fields.is_empty() { Vec::new() } else { bytes }', 'bytes'))
PLANTS.append((LANE_HOST, 'lane_host', "OpenCode's own files in the global folder move the digest",
               '    dependencies.remove(OPENCODE_PLUGIN);\n', ''))
PLANTS.append((LANE_SHIM, 'lane_host', 'a second idle of a held-open turn decides before its recheck',
               '        if matches!(seen, Ok(true)) && self.recheck.is_some() && !stopping {', '        if false {'))
# OpenCode's own compaction is no reply and no answer to a steer (issue 874).
PLANTS.append(('clarp-backend/src/opencode.rs', 'streams', "OpenCode's summary for itself is filed as a reply",
               '        if self.compactions.contains(msg) {', '        if false {'))
PLANTS.append((LANE_SHIM, 'lane_host', "OpenCode's compaction summary reads the steers before it",
               '            && !compaction(info)\n', '\n'))
PLANTS.append((HISTORY, 'history', "a past session's own compaction is a row",
               '        if opencode::compaction(data) {', '        if false {'))
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
