#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The tools the router of spoken messages asks, and how it asks each, as the Python Host's router does: the names a
provider is known by (the kernel's `router::provider`), the tools an app is offered and refused (`clarp-host`
`router.rs`), how each tool is started for one question and where its answer is (`Backend::once`, `answered`,
`own_home` in `clarp-backend`, and the arguments of OpenCode, Grok and Muse beside them), and the home of its own
the Host gives a tool that answers in one (`once.rs`). Held by `tests/router_tools_vectors.rs` (in the kernel and in
`clarp-backend`, over the vectors of the Python Host's own functions) and by the Host's test of a router that asks
Antigravity.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'router_tools_vectors'],
    'host': ['test', '--test', 'host', '--', 'the_router_asks_antigravity_or_any_other_tool_of_the_host'],
}
CLAUDE = 'clarp-backend/src/claude.rs'
CODEX = 'clarp-backend/src/codex.rs'
AGY = 'clarp-backend/src/agy.rs'
OPENCODE = 'clarp-backend/src/opencode.rs'
ARGS = 'clarp-tool-args/src/opencode.rs'
GROK = 'clarp-kernel/src/grok_muse.rs'
NAMES = 'clarp-kernel/src/router.rs'
ROUTER = 'clarp-host/src/router.rs'
ONCE = 'clarp-host/src/once.rs'
# NOT PLANTED, each with its reason:
#   the place of the question (standard input, a file, the command line): the port of the Python Host's PR 437,
#     another piece's; the vectors compare the arguments without it.
#   the ways Grok and Muse are asked and read, beyond Muse's own flag: held by their own vectors and list
#     (`backend-grok`).
CLAUDE_FLAGS = '''            "-p",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--strict-mcp-config",'''
CODEX_FLAGS = '''            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--skip-git-repo-check",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.is_empty());'''
CODEX_EFFORT = '''        if let Some(effort) = given(&question.effort) {
            args.push("-c".to_string());
            args.push(setting("model_reasoning_effort", &effort));
        }
        args.push("-".to_string());'''
CODEX_CHOICE = '''        if let Some(model) = given(&question.model) {
            args.push("--model".to_string());
            args.push(model);
        }
        if let Some(effort) = given(&question.effort) {
            args.push("-c".to_string());
            args.push(setting("model_reasoning_effort", &effort));
        }
        args.push("-".to_string());'''
CODEX_SWAPPED = '''        if let Some(effort) = given(&question.effort) {
            args.push("-c".to_string());
            args.push(setting("model_reasoning_effort", &effort));
        }
        if let Some(model) = given(&question.model) {
            args.push("--model".to_string());
            args.push(model);
        }
        args.push("-".to_string());'''
CODEX_HEAD = '''            "exec",
            "--json",
            "--sandbox",'''
AGY_ONCE = '''        let mut args: Vec<String> = ["--dangerously-skip-permissions", "--output-format", "json"]'''
AGY_CHOICE = '''        match (given(&question.model), given(&question.effort)) {
            (Some(model), _) => {
                args.push("--model".to_string());
                args.push(model);
            }
            (None, Some(effort)) => {
                args.push("--effort".to_string());
                args.push(effort.trim().to_lowercase());
            }
            (None, None) => {}
        }
        args.push(format!("--print={}", question.prompt));'''
TOOLS = '''    ("claude", "Claude"),
    ("codex", "Codex"),
    ("agy", "Antigravity"),'''
PLANTS = [
    # The names a provider is known by.
    (NAMES, 'vectors', 'a name keeps its blanks', '    let name = python::strip(raw.unwrap_or_default()).to_lowercase();', '    let name = raw.unwrap_or_default().to_lowercase();'),
    (NAMES, 'vectors', 'a name keeps its capitals', '    let name = python::strip(raw.unwrap_or_default()).to_lowercase();', '    let name = python::strip(raw.unwrap_or_default()).to_string();'),
    (NAMES, 'vectors', 'gpt is no name of the OpenAI API', '        "openai" | "gpt" => "openai".to_string(),', '        "openai" => "openai".to_string(),'),
    (NAMES, 'vectors', 'jev is no name of the typed model', '        "typesafe" | "jev" => "typesafe".to_string(),', '        "typesafe" => "typesafe".to_string(),'),
    (NAMES, 'vectors', 'claude-code is no name of Claude', '        "claude-code" => "claude".to_string(),', '        "claude-code-cli" => "claude".to_string(),'),
    # The tools an app is offered, and refused.
    (ROUTER, 'host', 'Antigravity is offered before Codex', TOOLS, '''    ("claude", "Claude"),
    ("agy", "Antigravity"),
    ("codex", "Codex"),'''),
    (ROUTER, 'host', 'Antigravity is no tool the router asks', TOOLS, '''    ("claude", "Claude"),
    ("codex", "Codex"),
    ("antigravity", "Antigravity"),'''),
    (ROUTER, 'host', 'Muse is offered by a short name', '    ("muse", "Muse Code"),', '    ("muse", "Muse"),'),
    (ROUTER, 'host', 'what a tool is offered as is said otherwise', '            "detail": format!("Runs an isolated {label} request on this Host."),', '            "detail": format!("Runs {label} on this Host."),'),
    (ROUTER, 'host', 'the refusal names two tools', '''            "unsupported orchestrator provider: {provider} (this Host asks a tool of its \\
             own, and has no API of a provider built in)"''', '''            "unsupported orchestrator provider: {provider} (this Host asks a tool of its \\
             own, codex or claude, and has no API of a provider built in)"'''),
    # Claude Code asked one question.
    (CLAUDE, 'vectors', 'Claude Code keeps the question as a session', CLAUDE_FLAGS, CLAUDE_FLAGS.replace('            "--no-session-persistence",\n', '')),
    (CLAUDE, 'vectors', 'Claude Code is asked with its MCP servers', CLAUDE_FLAGS, CLAUDE_FLAGS.replace('\n            "--strict-mcp-config",', '')),
    (CLAUDE, 'vectors', 'Claude Code is given its effort before its model', '        for (flag, value) in [("--model", &question.model), ("--effort", &question.effort)] {', '        for (flag, value) in [("--effort", &question.effort), ("--model", &question.model)] {'),
    # Codex asked one question, and its answer.
    (CODEX, 'vectors', 'Codex keeps the question', CODEX_FLAGS, CODEX_FLAGS.replace('            "--ephemeral",\n', '')),
    (CODEX, 'vectors', 'Codex refuses to start outside a repository', CODEX_FLAGS, CODEX_FLAGS.replace('            "--skip-git-repo-check",\n', '')),
    (CODEX, 'vectors', 'Codex may write anywhere', CODEX_FLAGS, CODEX_FLAGS.replace('"workspace-write"', '"danger-full-access"')),
    (CODEX, 'vectors', 'a model of blanks alone is not given to Codex', CODEX_FLAGS, CODEX_FLAGS.replace('.filter(|value| !value.is_empty())', '.filter(|value| !value.trim().is_empty())')),
    (CLAUDE, 'vectors', 'a model of blanks alone is not given to Claude Code', '        for (flag, value) in [("--model", &question.model), ("--effort", &question.effort)] {\n            if let Some(value) = value.as_ref().filter(|value| !value.is_empty()) {', '        for (flag, value) in [("--model", &question.model), ("--effort", &question.effort)] {\n            if let Some(value) = value.as_ref().filter(|value| !value.trim().is_empty()) {'),
    (CODEX, 'vectors', "Codex's effort is given bare", CODEX_EFFORT, CODEX_EFFORT.replace('setting("model_reasoning_effort", &effort)', 'format!("model_reasoning_effort={effort}")')),
    (CODEX, 'vectors', "Codex's model is given by its short flag", CODEX_CHOICE, CODEX_CHOICE.replace('"--model".to_string()', '"-m".to_string()')),
    (CODEX, 'vectors', "Codex's model is given without its blanks", CODEX_CHOICE, CODEX_CHOICE.replace('args.push(model);', 'args.push(model.trim().to_string());')),
    (CODEX, 'vectors', "Codex is given its effort before its model", CODEX_CHOICE, CODEX_SWAPPED),
    (CODEX, 'vectors', "Codex's effort is set under another key", CODEX_CHOICE, CODEX_CHOICE.replace('setting("model_reasoning_effort", &effort)', 'setting("reasoning_effort", &effort)')),
    (CODEX, 'vectors', "Codex's effort is given without its -c", CODEX_CHOICE, CODEX_CHOICE.replace('            args.push("-c".to_string());\n            args.push(setting', '            args.push(setting')),
    (CODEX, 'vectors', 'Codex prints no events', CODEX_HEAD, CODEX_HEAD.replace('            "--json",\n', '')),
    (CODEX, 'vectors', 'the first message of Codex is its answer', '        messages\n            .next_back()', '        messages\n            .next()'),
    (CODEX, 'vectors', 'a blank message of Codex is its answer', '            let text = item["text"].as_str().filter(|text| !text.trim().is_empty());\n            text.filter(|_| is_message).map(str::to_string)', '            let text = item["text"].as_str();\n            text.filter(|_| is_message).map(str::to_string)'),
    (CODEX, 'vectors', 'a message of Codex that is not over is its answer', '            let is_message = event["type"] == "item.completed" && item["type"] == "agent_message";\n            let text = item["text"].as_str().filter', '            let is_message = item["type"] == "agent_message";\n            let text = item["text"].as_str().filter'),
    (CODEX, 'vectors', 'no answer of Codex is said in other words', '            .ok_or_else(|| "codex orchestrator returned no agent message".to_string())', '            .ok_or_else(|| "codex returned no agent message".to_string())'),
    # Antigravity asked one question, in a home of its own, and its answer.
    (AGY, 'vectors', 'Antigravity is asked for plain words', AGY_ONCE, '        let mut args: Vec<String> = ["--dangerously-skip-permissions"]'),
    (AGY, 'vectors', 'a model of blanks alone is not given to Antigravity', '        let given = |value: &Option<String>| value.clone().filter(|value| !value.is_empty());\n        match (given(&question.model)', '        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());\n        match (given(&question.model)'),
    (AGY, 'vectors', 'Antigravity is given an effort beside its model', AGY_CHOICE, AGY_CHOICE.replace('''            (Some(model), _) => {
                args.push("--model".to_string());
                args.push(model);
            }''', '''            (Some(model), effort) => {
                args.push("--model".to_string());
                args.push(model);
                args.extend(effort.map(|effort| format!("--effort={effort}")));
            }''')),
    (AGY, 'vectors', "Antigravity's effort keeps its capitals and blanks", AGY_CHOICE, AGY_CHOICE.replace('args.push(effort.trim().to_lowercase());', 'args.push(effort);')),
    (AGY, 'vectors', 'Antigravity is given no effort', AGY_CHOICE, AGY_CHOICE.replace('''            (None, Some(effort)) => {
                args.push("--effort".to_string());
                args.push(effort.trim().to_lowercase());
            }
            (None, None) => {}''', '''            (None, _) => {}''')),
    (AGY, 'vectors', 'Antigravity asks before it acts', AGY_ONCE, AGY_ONCE.replace('"--dangerously-skip-permissions", ', '')),
    (AGY, 'vectors', 'Antigravity prints a stream of events', AGY_ONCE, AGY_ONCE.replace('"--output-format", "json"', '"--output-format", "stream-json"')),
    (AGY, 'vectors', "Antigravity's model is given by its short flag", AGY_CHOICE, AGY_CHOICE.replace('args.push("--model".to_string());', 'args.push("-m".to_string());')),
    (AGY, 'vectors', "Antigravity's model is given without its blanks", AGY_CHOICE, AGY_CHOICE.replace('args.push(model);', 'args.push(model.trim().to_string());')),
    (AGY, 'vectors', "Antigravity's effort is given by another flag", AGY_CHOICE, AGY_CHOICE.replace('args.push("--effort".to_string());', 'args.push("--reasoning-effort".to_string());')),
    (AGY, 'vectors', 'the question is given to Antigravity by another flag', AGY_CHOICE, AGY_CHOICE.replace('format!("--print={}", question.prompt)', 'format!("--prompt={}", question.prompt)')),
    (AGY, 'vectors', "Antigravity's answer is read from another field", '        match text_of(&said["response"]).trim() {', '        match text_of(&said["result"]).trim() {'),
    (AGY, 'vectors', 'what Antigravity prints before its answer is read as the answer', "        let from = printed.find('{');", "        let from = Some(0usize).filter(|_| printed.contains('{'));"),
    (AGY, 'vectors', 'what is copied into its home is linked', '''    copied: &[
        ".gemini/antigravity-cli/settings.json",''', '''    copied: &[
        ".gemini/antigravity-cli/settings-copy.json",'''),
    (AGY, 'vectors', 'its login is not linked into its home', '''    linked: &[
        ".gemini/oauth_creds.json",''', '''    linked: &['''),
    (AGY, 'vectors', 'an answer of Antigravity that ended in error is an answer', '            Value::String(status) if status == "SUCCESS" => {}', '            Value::String(_) => {}'),
    # OpenCode and DeepSeek asked one question.
    (OPENCODE, 'vectors', "OpenCode's question tool is not denied", '            env: vec![permissions()],\n            stdin: Vec::new(),\n        })\n    }\n    fn answered', '            env: Vec::new(),\n            stdin: Vec::new(),\n        })\n    }\n    fn answered'),
    (ARGS, 'vectors', 'OpenCode asks before it acts', '    let mut args = ["run", "--format", "json", "--auto"]', '    let mut args = ["run", "--format", "json"]'),
    (ARGS, 'vectors', "OpenCode's effort is given as an effort", '        ("--variant", effort),', '        ("--effort", effort),'),
    # Muse asked one question.
    (GROK, 'vectors', 'Muse keeps a log of the question', '            args.push("--no-session-log".into());', '            args.push("--session-log".into());'),
    # The home of its own a tool is given for one question.
    (ONCE, 'host', 'a tool that answers in a home of its own is asked in its user\'s', '            launch.env.push(("HOME".to_string(), home?));', '            let _ = home?;'),
    (ONCE, 'host', 'the home of a question is kept afterwards', '        let _ = std::fs::remove_dir_all(&self.0);', '        let _ = &self.0;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
