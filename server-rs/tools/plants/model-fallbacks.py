#!/usr/bin/env python3
"""Policy and one-shot model fallback mutations. All tool targets are stand-ins."""
import runner

RUNS={"vectors":["test","--test","model_fallbacks_vectors"],
      "host":["test","--test","host","--","inherited_models_retry_only_the_isolated_question_and_record_the_actual_model"]}
P="clarp-kernel/src/janitors/fallbacks.rs"
E="clarp-host/src/model_fallbacks.rs"
PLANTS=[]
def plant(file,run,why,old,new):
    PLANTS.append((file,run,why,old,new))

for word in ["Unsupported policy fields","Expected existing Janitor sessions","Duplicate inherited session",
             "At least one model is required","Unsupported model provider","Invalid provider model identifier",
             "Duplicate model","Inherited Janitors require a primary model","Deterministic Janitors do not inherit models",
             "Model provider incompatible with {session}","Policy changed; refresh before saving"]:
    file="clarp-kernel/src/janitors/book.rs" if word=="Invalid provider model identifier" else P
    old='"'+word+'"'
    if word in ("Expected existing Janitor sessions",):
        continue
    plant(file,"vectors","change the refusal for "+word,old,'"Invalid policy"')
for word in ["permission denied","approval required","not authorized","ownership lost","configuration changed"]:
    plant(P,"vectors","retry a provider after "+word,'"'+word+'"','"other refusal"')

for reason,old,new in [
    ("reverse requested revision equality","expected.as_u64() != Some(self.revision)","expected.as_u64() == Some(self.revision)"),
    ("accept an overlong identifier", "s.chars().count() > 200","s.chars().count() > 201"),
    ("reject a 200 character identifier","s.chars().count() > 200","s.chars().count() >= 200"),
    ("erase explicit policy entries on a patch","let mut next = self.clone();","let mut next = Self::default();"),
    ("freeze the policy revision","next.revision += 1;","next.revision += 0;"),
    ("do not keep a successful policy","*self = next;","let _ = next;"),
    ("forget one position in the chain","(index - 1, pick)","(index, pick)"),
    ("do not skip a repeated primary","pick != &primary","pick == &primary"),
    ("ignore the primary from global policy",'effective["source"] != "global"','effective["source"] == "global"'),
    ("fail to normalize denial case","message.to_lowercase()","message.to_string()"),
    ("accept model output over the size bound","text.chars().count() > 512_000","text.chars().count() > 512_001"),
    ("reject a model output exactly at the size bound","text.chars().count() > 512_000","text.chars().count() >= 512_000"),
    ("stop one brace too soon","match_indices('{').take(64)","match_indices('{').take(63)"),
    ("scan one brace too many","match_indices('{').take(64)","match_indices('{').take(65)"),
    ("accept the finishing tool envelope",'value.get("toolAction").is_none()','value.get("toolAction").is_some()'),
    ("accept the tool summary instead of the answer",'value.get("toolSummary").is_none()','value.get("toolSummary").is_some()'),
    ("ignore the answer schema","first_error(schema, &value).is_none()","first_error(schema, &value).is_some()"),
]:
    plant(P,"vectors",reason,old,new)
for reason,old,new in [
    ("claim the wrong receipt position",'"attempt":index','"attempt":index+1'),
    ("store a receipt as finished before the question",'"status":"running"','"status":"completed"'),
    ("misidentify the answering backend",'"backend":pick.backend','"backend":primary.backend'),
    ("misidentify the answering model",'"model":pick.model','"model":primary.model'),
    ("discard the reason for an alternate",'"reason":failure.reason','"reason":"none"'),
    ("write a response rather than a null result",'"result_json":null','"result_json":true'),
    ("perform fallback after a permission refusal","if !failure.retry {","if failure.retry {"),
    ("omit the suffix from the request key",'format!("{run}:{suffix}")','format!("{run}")'),
    ("lose successful model attribution","return Ok((value, pick))","return Ok((value, primary))"),
]:
    plant(E,"host",reason,old,new)
for why,old,new in [
    ("accept only the first of two supported fields",'.all(|k| matches!(k.as_str(), "model_chain" | "inherit_sessions"))','.any(|k| matches!(k.as_str(), "model_chain" | "inherit_sessions"))'),
    ("refuse an empty inheritance list", 'if sessions.iter().any(|s| janitor(s).is_none())','if sessions.is_empty() || sessions.iter().any(|s| janitor(s).is_none())'),
    ("do not persist inherited sessions", 'next.inherit_sessions = sessions;', 'next.inherit_sessions.clear();'),
    ("accept empty primary lists", '.filter(|r| !r.is_empty())','.filter(|_| true)'),
    ("ignore whitespace in an identifier", 's.trim().is_empty()','s.is_empty()'),
    ("change the fields accepted for one model", '#[serde(deny_unknown_fields)]',''),
    ("apply tool discovery to the openai entry", 'if model.provider == "openai"', 'if model.provider == "absent"'),
    ("discard a configured API model", 'models.push(model);','if model.provider != "openai" {models.push(model); }'),
    ("change duplicate chain error into revision conflict", 'bad("Duplicate model")','Refused::new(409,"revision_conflict","Duplicate model")'),
    ("check just the first provider of an inherited chain", 'next.model_chain.iter().all(supports)','next.model_chain.iter().take(1).all(supports)'),
    ("invent models for deterministic jobs", '"source":"deterministic","chain":[]','"source":"deterministic","chain":self.model_chain'),
    ("use policy revision for an explicit agent", '"source":"explicit","chain":[{"provider":agent.backend,"model":agent.model}],"revision":config.revision','"source":"explicit","chain":[{"provider":agent.backend,"model":agent.model}],"revision":self.revision'),
    ("use agent revision for inherited chains", '"source":"global","chain":self.model_chain,"revision":self.revision','"source":"global","chain":self.model_chain,"revision":config.revision'),
    ("grant default effort to inherited picks",'effort: String::new(),','effort: "low".into(),'),
    ("retry after a user interruption", '!= crate::failures::INTERRUPTED','== crate::failures::INTERRUPTED'),
    ("stop scanning after a malformed object", 'let parsed = serde_json::Deserializer','if text[at..].starts_with("{bad") {return None;} let parsed = serde_json::Deserializer'),
]:
    plant(P,"vectors",why,old,new)

for file,why,old,new in [
    (E,"forget interrupted fallback claims",'.kept_book(KIND, key.clone())','.kept_book(KIND, format!("other-{key}"))'),
    (E,"store a failed alternate as cancelled",'Err(e) if e.retry => "failed"','Err(e) if e.retry => "cancelled"'),
    (E,"store a cancelled alternate as completed",'Err(_) => "cancelled"','Err(_) => "completed"'),
    (E,"remove the agent from the claim key","json!([agent, request, index])","json!([request, index])"),
    (E,"use the primary backend on all retries","invoke(pick.clone())","invoke(primary.clone())"),
    ("clarp-host/src/keeper.rs","attribute the keeper update to its configured model",'receipt["model"] = json!(actual.model);','receipt["model"] = json!(run.model);'),
    ("clarp-host/src/explain.rs","read with the primary for every explanation",'once::read_on(app, &pick.backend, &reading, within)','once::read_on(app, "absent", &reading, within)'),
    ("clarp-host/src/router.rs","mix initial and broad attempt keys",'ask(app, &routing, "initial")','ask(app, &routing, "broad")'),
    ("clarp-host/src/janitors.rs","drop the frozen primary for managed turns","        managed_model,","        managed_model: managed_model.filter(|_|false),"),
    ("clarp-host/src/models.rs","reject an unobserved Codex catalogue",'(tool == "agy").then_some(&bundled)','(tool != "agy").then_some(&bundled)'),
    ("clarp-host/src/janitors.rs","hide the effective-chain read route",'if what == "effective-model-chain"','if what == "missing-model-chain"'),
    ("clarp-kernel/src/janitors/runs.rs","freeze the explicit chain on scheduled turns",'"config": trigger.config, "effective_chain": self.model_policy.effective(&agent.session,&agent,config)','"config":trigger.config,"effective_chain":null'),
    ("clarp-kernel/src/janitors/demand.rs","omit the chain from demand-run configuration",'"effective_chain": self.model_policy.effective(&agent.session,&agent,config)','"effective_chain": null'),
]:
    plant(file,"host",why,old,new)

I="clarp-kernel/src/janitors/routing_input.rs"
O="clarp-host/src/once.rs"
for why,old,new in [
    ("count characters rather than UTF8 bytes","arg.len() >= 128 * 1024","arg.chars().count() >= 128 * 1024"),
    ("allow the kernel limit without space for the NUL","arg.len() >= 128 * 1024","arg.len() > 128 * 1024"),
    ("refuse the last valid argument","arg.len() >= 128 * 1024","arg.len() >= 128 * 1024 - 1"),
    ("rewrite commands whose prompt is elsewhere","last == prompt","last != prompt"),
    ("leave Claude prompt inline",'"claude" | "codex" =>','"codex" =>'),
    ("leave Codex prompt inline",'"claude" | "codex" =>','"claude" =>'),
    ("drop Codex stdin marker",'args.push("-".into());','args.push("--".into());'),
    ("leave Grok prompt inline",'"grok" | "muse" =>','"muse" =>'),
    ("leave Muse prompt inline",'"grok" | "muse" =>','"grok" =>'),
    ("forget a bare flag and prompt pair","args.len() >= 2","args.len() > 2"),
    ("ignore the flag immediately before the prompt","args[args.len() - 2] == flag","args[args.len() - 2] != flag"),
    ("keep the old prompt flag","args.truncate(args.len() - 2)","args.truncate(args.len() - 1)"),
    ("misname the prompt file option",'args.push("--prompt-file".into())','args.push("--file".into())'),
    ("change the argument-only size refusal","the CLI takes it only as an argument","the CLI refused the answer"),
]:
    plant(I,"vectors",why,old,new)
for tool in ["claude","codex"]:
    plant(O,"host",f"copy the {tool} stdin prompt into argv as well","if !launch.stdin.is_empty() {",
          f'if !launch.stdin.is_empty() {{ if tool=="{tool}" {{launch.args.push(prompt.to_string());}}')
for tool in ["grok","muse"]:
    plant(O,"host",f"send the {tool} prompt on argv","    match routing_input::prepare(tool,",
          f'    if tool=="{tool}" {{return Ok(None);}}\n    match routing_input::prepare(tool,')
plant(O,"host","retain the private routing file after the call","let _ = std::fs::remove_dir_all(&self.0);",
      'if !self.0.join("prompt.txt").exists() {let _ = std::fs::remove_dir_all(&self.0);}')
# The run is told the tool's name and its question since routing-failure-445 (R85).
plant(O,"host","check size only after trying to start an argument-only tool",
      "    let _prompt_file = prepare(routing_root, backend, &mut launch, &question.prompt)?;\n    let printed = run(&launch, cwd, within, (backend, &question.prompt)).await;",
      '    let printed = if backend=="opencode"{run(&launch,cwd,within,(backend,&question.prompt)).await}else{let _file=prepare(routing_root,backend,&mut launch,&question.prompt)?;run(&launch,cwd,within,(backend,&question.prompt)).await};')
for why,old,new in [
    ("encode a low surrogate outside the escape range","0xdc80..=0xdcff","0xdc7f..=0xdcff"),
    ("refuse the first escaped byte","0xdc80..=0xdcff","0xdc81..=0xdcff"),
    ("refuse the last escaped byte","0xdc80..=0xdcff","0xdc80..0xdcff"),
    ("encode the next unpaired surrogate as a byte","0xdc80..=0xdcff","0xdc80..=0xdd00"),
    ("shift each escaped byte","n - 0xdc00","n - 0xdbff"),
    ("change content-free encoding failure words","prompt cannot be encoded for {backend}","{backend} cannot start"),
    ("omit the backend on timeout","{backend} timed out after {duration} s","timed out after {duration} s"),
    ("print too few significant digits","{seconds:.5e}","{seconds:.4e}"),
    ("show a full executable path","rsplit('/')","split('/')"),
    ("misname an unknown historical executable","the routing CLI","the model"),
]: plant(I,"vectors",why,old,new)
plant("clarp-kernel/src/router/flow.rs","host","serve historical command-bearing timeout text",
      'json!(crate::janitors::routing_input::public_error(&self.error))','json!(self.error)')
plant("clarp-host/src/main.rs","host","skip prompt cleanup during Host startup",
      'clarp_host::once::sweep_routing(&config.run_dir, std::time::SystemTime::now());','let _ = &config.run_dir;')
plant(O,"host","remove an unrelated directory in the Host run directory",
      's.strip_prefix("clarp-routing-")','Some(s.trim_start_matches("clarp-routing-"))')
RUNS["sweep"]=["test","--lib","a_prompt_directory_stays_through_its_last_live_instant"]
plant(O,"sweep","sweep at exactly one hour","age > Duration::from_secs(3600)","age >= Duration::from_secs(3600)")
plant(O,"sweep","wait an extra second before removing a stale prompt","age > Duration::from_secs(3600)","age > Duration::from_secs(3601)")
plant("clarp-host/src/explain.rs","host","classify a converted tool explanation timeout as translator failure",
      '"timeout" => "timeout",','"timeout" => "translator_failed",')
plant(E,"host","lose the timeout category when a lower layer rewrites its words",
      'Unanswered::TimedOut | Unanswered::RoutingTimeout(_) => "timeout",','Unanswered::TimedOut => "timeout",\n            Unanswered::RoutingTimeout(_) => "tool_failed",')
plant("clarp-kernel/src/devices.rs", "host", "let a limited device set the Janitor policy",
      'method != "POST" || !LIMITED_POSTS.contains(&path)',
      'method != "POST" || (path != "/janitor-policy" && !LIMITED_POSTS.contains(&path))')
plant("clarp-kernel/src/devices.rs", "host", "let a limited device read the Janitor policy",
      'return !LIMITED_GETS.contains(&path)',
      'return path != "/janitor-policy" && !LIMITED_GETS.contains(&path)')
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
