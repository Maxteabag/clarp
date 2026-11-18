#!/usr/bin/env python3
"""Compare the two backends' commands, streams, imported rows and picker to their oracles."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'backend_grok_vectors'],
    'shim': ['test', '--test', 'shim', '--', 'prompt_delivery_is_selected_by_the_shim_flag'],
    'bounds': ['test', '-p', 'clarp-backend', '--lib', 'composed_prompt_byte_bounds_match'],
    'stream': ['test', '-p', 'clarp-backend', '--lib', 'recorded_streams_follow'],
    'catalogue': ['test', '-p', 'clarp-backend', '--lib', 'model_lists_match'],
    'host': ['test', '--test', 'host', '--', 'grok_and_muse_run_stop_and_resume'],
}
K = 'clarp-kernel/src/grok_muse.rs'
H = 'clarp-kernel/src/grok_muse_history.rs'
B = 'clarp-backend/src/grok_muse.rs'
C = 'clarp-backend/src/catalogue.rs'
PLANTS = []


def change(file, run, why, before, after):
    PLANTS.append((file, run, why, before, after))


def word(file, run, literal):
    # Every spelling is independently observable in an argv or recorded event.
    change(file, run, 'the spelling of '+literal+' changes', '"'+literal+'"', '"'+literal+'_wrong"')


for flag in ('--always-approve', '--no-alt-screen', '--output-format', 'streaming-json',
             'exec', '--json','--yolo','--resume','--session-id','--reasoning-effort',
             '--model','-p','--','--no-session-log'):
    word(K,'vectors',flag)

for key in ('method','session/update','params','update','sessionUpdate','agent_message_chunk',
            'data','role','model','output_text','run.output.delta','run.terminal.completed'):
    word(K,'vectors',key)

for why,before,after in [
        ('a fresh session resumes','self == Self::Grok && !fresh','self == Self::Grok && fresh'),
        ('empty options are emitted','if !value.is_empty()', 'if value.is_empty()'),
        ('the value precedes its flag','[flag.to_string(), value.to_string()]','[value.to_string(), flag.to_string()]'),
        ('the answer loses previous deltas','text.push_str(&self.delta(&event));','text = self.delta(&event);'),
        ('a one-shot loses its supplied model','self.arguments("", false, model, effort)','self.arguments("", false, effort, model)'),
        ('the terminal answer is discarded','return answer.into();','return text;'),
        ('text data is ignored','return data.into();','return String::new();'),
        ('fallback fields change order','["content", "text", "message", "delta"]','["delta", "message", "text", "content"]'),
        ('a user is accepted as a model','"" | "assistant" | "model"','"" | "assistant" | "user"'),
        ('the outer event spelling is ignored','word(&event["event"])','word(&event["wrong"])'),
        ('empty content shadows another field','.find(|s| !s.is_empty())','.next()'),
        ('nested parts are concatenated without newlines','.join("\\n")','.join("")'),
        ('text content as a string is ignored','value["content"].is_string() || value["content"].is_array()','value["content"].is_array()'),
        ('text content as an array is ignored','value["content"].is_string() || value["content"].is_array()','value["content"].is_string()')]:
    change(K,'vectors',why,before,after)

for name in ('run_terminal_command','read_file','write','write_file','search_replace','edit_file','apply_patch','shell',
             'exec_command','grep','glob','list_dir','todo_write','web_search','web_fetch','target_file','target_directory',
             'cmd','backend_tool_call','tool_use','exit: ','<user_query>','</user_query>','<user_info>','runtime.session',
             'assistant_tool_calls_committed','tool_result_batch_committed','assistant_message_committed','commentary',
             'final_answer','completed','cancelled','interrupted','tool_use_id','synthetic_reason'):
    word(H,'vectors',name)

for why,before,after in [
        ('numeric microsecond timestamp discarded','let micros = r["recorded_at"]','let micros = r["wrong_timestamp"]'),
        ('opposite exit code success' ,'&rest[..digits + minus] != "0"','&rest[..digits + minus] == "0"'),
        ('pending card marked complete','row["status"] = json!("pending")','row["status"] = json!("ok")'),
        ('the session timestamp loses precision','format!("{t:.6}")','format!("{t:.3}")'),
        ('failed row marked successful','failed["failed"] = json!(true)','failed["failed"] = json!(false)'),
        ('an assistant call gets user role','row("assistant", answer, std::mem::take','row("user", answer, std::mem::take'),
        ('unmatched result overwrites another tool','(id.is_empty() || c["id"] == id)','(id.is_empty() || c["id"] != id)'),
        ('attachments accumulated after a final answer disappear','append_tools(turns.get_mut(*prev), &mut pending);','pending.clear();')]:
    change(H,'vectors',why,before,after)

for name in ('run.model.configured','model_id','run.usage','models','run.terminal.','aborted','reason','turn_failed','turn.failed'):
    word(B,'stream',name)

for why,before,after in [
        ('model id remains unset','Signal::Model(word(&p["model_id"]).into())','Signal::Model(String::new())'),
        ('muse inputs and outputs swapped','tokens_in.saturating_add(input)','tokens_in.saturating_add(1)'),
        ('muse outputs and inputs swapped','tokens_out.saturating_add(output)','tokens_out.saturating_add(1)'),
        ('grok input count is its output','tokens(&usage["input_tokens"])','tokens(&usage["output_tokens"])'),
        ('grok output count is its input','tokens(&usage["output_tokens"])','tokens(&usage["input_tokens"])'),
        ('a new delta replaces the paragraph','self.text.push_str(&delta);','self.text = delta;')]:
    change(B,'stream',why,before,after)

for name in ('grok models','muse model catalogue cache'):
    word(C,'catalogue',name)
for why,before,after in [
        ('the grok header becomes a model','line.starts_with("You are")','line.starts_with("nobody")'),
        ('the default headline becomes a model','line.starts_with("Default ")','line.starts_with("nobody")'),
        ('invisible muse variants are listed','row["visibility"] != "visible"','row["visibility"] == "visible"'),
        ('model effort rows are ignored','v["tier"]','v["wrong"]'),
        ('model labels come from a different column','row["display_label"]','row["wrong_label"]')]:
    change(C,'catalogue',why,before,after)

for why,before,after in [
        ('long prompts use the wrong file flag','args.push("--prompt-file".into());','args.push("--wrong-file".into());'),
        ('tool result text is discarded','call.output = Some(shorten(&text, 4000));','call.output = None;'),
        ('a completed tool reports failure','call.status = if ok','call.status = if !ok'),
        ('resumed sessions are fresh','spec.backend_session.is_none(),','spec.backend_session.is_some(),')]:
    change(B,'host',why,before,after)

for why,before,after in [
        ('exactly 64 KiB unnecessarily uses a file','prompt.len() > 64 * 1024','prompt.len() >= 64 * 1024'),
        ('one byte too many stays on argv','prompt.len() > 64 * 1024','prompt.len() > 64 * 1024 + 1'),
        ('non-ASCII characters counted instead of bytes','prompt.len() > 64 * 1024','prompt.chars().count() > 64 * 1024'),
        ('private file transport loses the prompt bytes','stdin = prompt.into_bytes();','stdin = vec![1];'),
        ('the turn runs under a different name','spec.session.clone())','String::new())')]:
    change(B,'bounds',why,before,after)

A = 'clarp-host/src/logins.rs'
P = 'clarp-backend/src/past.rs'
for why,before,after in [
        ('a signed-out CLI reports a valid session','!said.to_lowercase().contains("not authenticated")','said.to_lowercase().contains("not authenticated")'),
        ('a valid session reports no credential','row["credential_present"] = json!(valid);','row["credential_present"] = json!(false);'),
        ('CLI failure reports signed-out','else if error.is_empty()','else if !error.is_empty()'),
        ('an installed Muse has the wrong auth state','"host_managed"','"unverified"'),
        ('an all-harnesses request drops non-default tools','every || matches!(*tool, "claude" | "codex")','matches!(*tool, "claude" | "codex")')]:
    change(A,'host',why,before,after)
for why,before,after in [
        ('year directory needs three digits','date_dirs(base, 4)','date_dirs(base, 3)'),
        ('month directory needs one digit','date_dirs(&y, 2)','date_dirs(&y, 1)'),
        ('day directory needs one digit','date_dirs(&m, 2)','date_dirs(&m, 1)'),
        ('the grok store has a different default path','"CLAUDE_PWA_GROK_HOME", ".grok"','"CLAUDE_PWA_GROK_HOME", ".grok-missing"'),
        ('session preview is cut to one character','python::first(&preview,240)','python::first(&preview,1)'),
        ('a session title is shown as its preview','"preview":python::first(&preview,240)','"preview":title.clone()')]:
    change(P,'host',why,before,after)
for key in ('turn_started','tool_call_update','run.lifecycle.started','thought','turn.started'):
    # Only change the stream dispatch. The test compares the wire phase for each line.
    word(B,'stream',key)

for why,before,after in [
        ('the router asks the effort as the model','q.model.as_deref().unwrap_or("")','q.effort.as_deref().unwrap_or("")'),
        ('the router asks the model as the effort','q.effort.as_deref().unwrap_or("")','q.model.as_deref().unwrap_or("")'),
        ('the routing question is empty','&q.prompt,','"",'),
        ('an assigned session is not recorded','fn binds_at_spawn(&self) -> bool {\n        true','fn binds_at_spawn(&self) -> bool {\n        false'),
        ('a Muse lock has no recovery classification','muse_session_locked: Muse session','session_busy: Muse session')]:
    change(B,'host',why,before,after)

S = 'clarp-shim/src/main.rs'
for why,before,after in [
        ('a file prompt is the default','let mut prompt_as_file = false;', 'let mut prompt_as_file = true;'),
        ('the shim ignores its file flag','=> prompt_as_file = true,','=> prompt_as_file = false,'),
        ('a file prompt needs a named session','if let Some(id) = args.named_session {','if args.prompt_as_file && args.named_session.is_none() { return Err(String::from("session required")); }\n    if let Some(id) = args.named_session {'),
        ('the turn input path is appended twice','.args(prompt_path)','.args(prompt_path).args(prompt_path)'),
        ('the wrong path is appended','.args(prompt_path)','.args(prompt_path.map(|path| path.with_extension("wrong")))'),
        ('the file flag selects piped stdin','.stdin(if args.prompt_as_file {','.stdin(if !args.prompt_as_file {'),
        ('ordinary stdin loses the turn bytes','.filter(|_| !args.prompt_as_file)','.filter(|_| args.prompt_as_file)')]:
    change(S,'shim',why,before,after)
change('clarp-actor/src/lib.rs','host','the actor drops file delivery','prompt_as_file: launch.prompt_as_file,','prompt_as_file: false,')
change('clarp-host/src/launcher.rs','host','the launcher omits the file flag','if order.prompt_as_file {','if !order.prompt_as_file {')

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
