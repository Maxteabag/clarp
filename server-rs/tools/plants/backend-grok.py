#!/usr/bin/env python3
"""Compare the two backends' commands, streams, imported rows and picker to their oracles."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'backend_grok_vectors'],
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
        ('muse inputs and outputs swapped','tokens_in.saturating_add(row["input_tokens"]','tokens_in.saturating_add(row["output_tokens"]'),
        ('muse outputs and inputs swapped','tokens_out.saturating_add(row["output_tokens"]','tokens_out.saturating_add(row["input_tokens"]'),
        ('grok input count is its output','usage["input_tokens"].as_u64()','usage["output_tokens"].as_u64()'),
        ('grok output count is its input','usage["output_tokens"].as_u64()','usage["input_tokens"].as_u64()'),
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
        ('long prompts use the wrong file flag','args.extend(["--prompt-file".into(),','args.extend(["--wrong-file".into(),'),
        ('tool result text is discarded','call.output = Some(shorten(&text, 4000));','call.output = None;'),
        ('a completed tool reports failure','call.status = if ok','call.status = if !ok'),
        ('resumed sessions are fresh','spec.backend_session.is_none(),','spec.backend_session.is_some(),')]:
    change(B,'host',why,before,after)

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
