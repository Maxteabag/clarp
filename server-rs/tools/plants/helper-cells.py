"""Wrong rules for Claude in-process helper cells."""
import runner

RUNS={"vectors":["test","--test","helper_cells"],"host":["test","--test","host","a_helpers_cell_tracks_its_parent_call"]}
PLANTS=[
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'legacy Task unrecognized', '"Agent" | "Task"', '"Agent" | "task"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'Agent unrecognized', '"Agent" | "Task"', '"agent" | "Task"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'null value is printed', '!python::truthy(v)', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'leading whitespace remains', 'filter(|w| !w.is_empty())', 'filter(|_|true)'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'task words run together', 'join(" ")', 'join("")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'ellipsis lost', 'format!("{first}…")', 'format!("{first}.")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'description smaller', '&text(&input["description"]), 120', '&text(&input["description"]),119'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'description larger', '&text(&input["description"]), 120', '&text(&input["description"]),121'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'prompt smaller', '&text(&input["prompt"]),240', '&text(&input["prompt"]),239'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'prompt larger', '&text(&input["prompt"]),240', '&text(&input["prompt"]),241'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'role untrimmed', 'python::strip(&role)', 'role.as_str()'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'summary skips description', '!description.is_empty()', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'summary skips type', '!role.is_empty()', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'default label changed', 'else {\n        "agent"\n    }', 'else{"helper"}'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'different call id', '"id":id', '"id":"lost"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'wrong cell kind', '"kind":"subagents"', '"kind":"tool"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'ephemeral flag lost', '"ephemeral":true', '"ephemeral":false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'background accepts any truthy field', 'input["run_in_background"]==true', 'python::truthy(&input["run_in_background"])'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'agent identity not recorded', 'if let Some(id) = meta["agentId"].as_str() {\n        cell["agent_id"] = json!(id);', 'if let Some(id)=meta["agentId"].as_str(){cell["agent_id"]=json!(format!("wrong-{id}"));'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'snake error flag ignored', 'python::truthy(&block["is_error"])', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'camel error flag ignored', 'python::truthy(&block["isError"])', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'async status ignored', 'meta["status"] == "async_launched"', 'false'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'async boolean coerced', 'meta["isAsync"] == true', 'python::truthy(&meta["isAsync"])'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'failed result called okay', '"failed" | "error" | "killed"', '"absent"|"error"|"killed"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'errored result called okay', '"failed" | "error" | "killed"', '"failed"|"absent"|"killed"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'killed result called okay', '"failed" | "error" | "killed"', '"failed"|"error"|"absent"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'stopped foreground result called failed', '"failed" | "error" | "killed"', '"failed"|"error"|"killed"|"stopped"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'wrong tool count suffix', 'count == 1', 'count==0'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'seconds rounded up', '(ms / 1000.0).floor()', '(ms/1000.0).ceil()'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'minute begins late', 'seconds >= 60', 'seconds>60'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'minute begins early', 'seconds >= 60', 'seconds>=59'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'usage separator changed', 'stats.join(" · ")', 'stats.join(" ")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'notification field not stripped', 'python::strip(s)', 's'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'status not case folded', 'field(body, "status").to_lowercase()', 'field(body,"status").to_owned()'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'notification cannot find parent', 'field(body, "tool-use-id")', 'field(body,"task-id")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'status failed ignored', '"completed" | "failed" | "killed" | "stopped"', '"completed"|"absent"|"killed"|"stopped"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'status killed ignored', '"completed" | "failed" | "killed" | "stopped"', '"completed"|"failed"|"absent"|"stopped"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'status stopped ignored', '"completed" | "failed" | "killed" | "stopped"', '"completed"|"failed"|"killed"|"absent"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'unclosed envelope discarded', 'unwrap_or((rest, ""))', 'unwrap_or(("",""))'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'second notification ignored', 'text = tail;', 'text="";'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'empty result ignores fallback', 'field(body, "summary")', 'field(body,"missing")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'discard task id', 'field(body, "task-id")', 'field(body,"missing")'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'receipt id overwritten by notice', '!id.is_empty() && cell["agent_id"] == ""', '!id.is_empty()'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'stopped title lost', '"Agent stopped"', '"Agent failed"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'failure title lost', '"Agent failed"', '"Agent finished"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'finished title lost', '"Agent finished"', '"Agent done"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'stopped flag wrong', 'status == "stopped"', 'status=="failed"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'notification output below limit', 'one_line(result, 240)', 'one_line(result,239)'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'task line label changed', '("Task", cell', '("Prompt",cell'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'type line kind changed', 'cell["subagent_type"].as_str(), "muted"', 'cell["subagent_type"].as_str(),"detail"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'usage line kind changed', 'cell["_stats"].as_str(), "muted"', 'cell["_stats"].as_str(),"detail"'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'wrong mode when background', '{\n                "Background"\n            } else {\n                "Foreground"\n            }', '{"Foreground"}else{"Background"}'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'missing paths produce an empty line', 'content.filter(|s| !s.is_empty())', 'content'),
    ('clarp-kernel/src/helper_cells.rs', 'vectors', 'private state appears in reply', "!name.starts_with('_')", 'true'),
    ('clarp-host/src/helper_source.rs', 'host', 'call identity fails to join the metadata', 'meta["toolUseId"]', 'meta["wrongId"]'),
    ('clarp-host/src/helper_source.rs', 'host', 'child filename prefix is different', 'name.strip_prefix("agent-")', 'name.strip_prefix("not-agent-")'),
    ('clarp-host/src/helper_source.rs', 'host', 'child files are never recorded', 'suffix.strip_suffix(".jsonl")', 'suffix.strip_suffix(".missing")'),
    ('clarp-host/src/helper_source.rs', 'host', 'metadata extension is ignored', 'suffix.strip_suffix(".meta.json")', 'suffix.strip_suffix(".absent")'),
    ('clarp-host/src/helper_source.rs', 'host', 'old alias always remains', 'lines.retain(|line| line["label"] != "Transcript")', 'lines.retain(|_|true)'),
    ('clarp-host/src/helper_source.rs', 'host', 'refreshed transcript line has wrong label', '"label":"Transcript","text":path', '"label":"Child","text":path'),
    ('clarp-host/src/helper_source.rs', 'host', 'parent reread loses completion', 'helper_cells::returned(c, b, &r["toolUseResult"])', '{}'),
    ('clarp-backend/src/history.rs', 'vectors', 'the result of a helper is not associated', 'part["tool_use_id"]\n                                .as_str()', 'part["wrong_id"].as_str()'),
    ('clarp-backend/src/history.rs', 'vectors', 'a queue-operation is never applied', 'record["type"] == "queue-operation"', 'record["type"]=="absent"'),
    ('clarp-backend/src/history.rs', 'vectors', 'an empty clause drops helper-only rows', 'text.is_empty() && cards.is_empty() && cells.is_empty()', 'text.is_empty() && cards.is_empty()'),
    ('clarp-backend/src/history.rs', 'vectors', 'notifications cannot reach earlier calls', 'calls.get(id)\n            && let Some(cell)', 'calls.get("never") && let Some(cell)'),
    ('clarp-host/src/helper_source.rs', 'host', 'wrong inferred agent id', 'cell["agent_id"] = json!(agent)', 'cell["agent_id"]=json!(format!("other-{agent}"))'),
    ('clarp-host/src/helper_source.rs', 'host', 'labels are never withdrawn', 'cell["transcript_path"] = json!(child.map(String::as_str).unwrap_or(""))', 'cell["transcript_path"]=json!(child.map(String::as_str).unwrap_or("stale"))'),
    ('clarp-host/src/helper_source.rs', 'host', 'a child symlink counts as a transcript', 'if !entry.file_type().is_ok_and(|t| t.is_file())', 'if entry.file_type().is_ok_and(|t|t.is_dir())'),
    ('clarp-host/src/wire.rs', 'host', 'a helper is duplicated as a tool card', 'filter(|c| c.cell.is_none())', 'filter(|_|true)'),
    ('clarp-host/src/wire.rs', 'host', 'a helper cell loses its compact count', 'details(cell)))', '0))'),
    ('clarp-host/src/edge.rs', 'host', 'the helper cells feature has the wrong contract', '("claude_subagent_cells", 14)', '("claude_subagent_cells",13)'),
    ('clarp-actor/src/helper_cells.rs', 'host', 'a revised transcript is not a conversation change', 'out.changed.conversation = true', 'out.changed.conversation=false'),
    ('clarp-actor/src/helper_cells.rs', 'host', 'metadata changes are ignored for imported rows', 'update(cell, true, None)', '{}'),
    ('clarp-backend/src/claude.rs', 'host', 'the stream returns no helper projection', 'self.helpers.insert(call_id.to_owned(), state);\n                            Some(cell)', 'self.helpers.insert(call_id.to_owned(),state);None'),
    ('clarp-actor/src/helper_cells.rs', 'host', 'a receipt cannot find its parent message', 'row = entry.part_of().unwrap_or(id).to_owned()', 'row=id.to_owned()'),
    ('clarp-actor/src/conversation.rs', 'host', 'omit imported helper updates from full pages', 'current(self.log, &import.id, item.seq)', 'current(self.log,"unrelated",item.seq)'),
    ('clarp-actor/src/conversation.rs', 'host', 'omit live helper association updates from the reply', 'current(self.log, &call.id, stored.seq)', 'current(self.log,"unrelated",stored.seq)'),
    ('clarp-host/src/agent.rs', 'host', 'a helper change omits its native session', 'event["backend_session_id"] = Value::String(session)', 'event["backend_session_id"]=Value::Null'),
]
RUNS["reads"]=["test","--lib","helper_refresh_"]
F="clarp-host/src/helper_source.rs"
for label,old,new in [
    ("idle agents keep polling","live.then(||","true.then(||"),
    ("end does not trigger its last listing","if self.live && !live {","if false {"),
    ("each refresh reads every metadata file again","|(was, _)| *was == stamp","|_| false"),
    ("a changed sidecar keeps a cached alias","|(was, _)| *was == stamp","|_| true"),
    ("ignore the metadata size bound","stamp.bytes > META_BYTES","false"),
    ('an append starts a full transcript parse', 'old.bytes == stamp.bytes && old != stamp', 'old.bytes!=stamp.bytes || old!=stamp'),
    ('seek back to zero when the parent grows', 'file.seek(SeekFrom::Start(start))', 'file.seek(SeekFrom::Start(0))'),
    ('read a live-only parent without imported cells', 'if request.parent {\n            next.min(self.parent.offset)\n        }', 'if true {next.min(self.parent.offset)}'),
    ('forget a partial appended line', 'self.lines = lines;', 'self.lines=Default::default();'),
]:
    PLANTS.append((F,"reads",label,old,new))
# Whether a reader holds a conversation that ended (clarp-actor's conversation.rs, Page::ended_for): asked by the
# route of GET /log and by the follower of clarp-actor's tests, which holds what a reload reads after every change.
RUNS["page"]=["test","-p","clarp-actor","--lib","a_reader_is_told_its_conversation_ended"]
RUNS["follow"]=["test","--test","actor","a_follower_by_revision_holds"]
PLANTS += [
    ('clarp-actor/src/conversation.rs', 'follow', 'a reader above the head is not told its conversation ended', 'held > self.head_revision || held <= self.begins_after', 'held <= self.begins_after'),
    ('clarp-actor/src/conversation.rs', 'page', 'a reader at the floor is not told its conversation ended', 'held > self.head_revision || held <= self.begins_after', 'held > self.head_revision || held < self.begins_after'),
    # A page of changes gives a row only when it reaches the row's newest part, in the order of revisions.
    ('clarp-actor/src/conversation.rs', 'follow', 'a row with a part beyond the page is given on it', '        entries.retain(|row| row.revision <= cursor);\n', ''),
    ('clarp-actor/src/conversation.rs', 'follow', 'a page of changes not in the order of revisions', '    entries.sort_by_key(|row| row.revision);\n', ''),
    ('clarp-actor/src/conversation.rs', 'follow', 'a page of changes with more to come may be empty', 'if entries.is_empty() && cursor / REVISION_STEP > from {', 'if false {'),
    # A row has one time: a call that is a row by itself, written again, keeps its first writing's.
    ('clarp-actor/src/lib.rs', 'follow', "a call row written again at its end's time", '*live.first_written.entry(call.id.clone()).or_insert(t)', 't'),
    ('clarp-actor/src/lib.rs', 'follow', "a call row cut off at the cut's time", 'None => first_written.get(&call.id).copied().unwrap_or(ts_ms),', 'None => ts_ms,'),
    ('clarp-actor/src/lib.rs', 'follow', 'a call row first written at the restart, as a restart reads it', 'first_written.entry(call.id.clone()).or_insert(record.t);', 'first_written.entry(call.id.clone()).or_insert(now_ms);'),
]
# The other agents a row names, by the roster when the row is read (clarp-host wire.rs, fleet.rs).
RUNS["names"]=["test","-p","clarp-host","--lib","the_agents_a_row_names"]
RUNS["writes"]=["test","--test","host","an_agent_writes_to_another_agent_with_its_own_tool"]
PLANTS += [
    ('clarp-host/src/wire.rs', 'names', "a user's row names its sender as kept", '            ("sender_agent_id", "sender_name", "sender_session"),\n', ''),
    ('clarp-host/src/wire.rs', 'names', 'a known agent named as kept', 'if let Some((named, at)) = shown_as(agent_id) {', 'if let Some((named, at)) = shown_as(agent_id).filter(|_| false) {'),
    ('clarp-host/src/fleet.rs', 'writes', 'a removed agent is not named', 'None => self.gone.get(agent_id).cloned(),', 'None => None,'),
    ('clarp-host/src/fleet.rs', 'writes', 'an agent removed now is forgotten', '        self.gone.insert(\n            record.agent_id.clone(),', '        let _ = (\n            record.agent_id.clone(),'),
    ('clarp-host/src/fleet.rs', 'writes', 'an agent removed before the start is forgotten', '            gone.insert(\n                record.agent_id.clone(),\n                (record.persona.clone(), record.session.clone()),\n            );\n            if !record.ledger_retired {', '            if !record.ledger_retired {'),
]
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
