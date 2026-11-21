#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Signing a tool in and out from an app (`backend_auth`): the kernel's rules of what an app is shown of a sign-in, why
it failed and what comes of a code pasted back, held to the Python Host's own answers; and the Host that runs the
tools' own sign-ins where it keeps the machine's tools, and refuses every change where it does not.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'backend_auth_vectors'],
    'host': ['test', '--test', 'host', 'backend_auth::'],
}
K = 'clarp-kernel/src/sign_in.rs'
H = 'clarp-host/src/logins/sign_in.rs'
E = 'clarp-host/src/edge.rs'
# NOT PLANTED, each with its reason:
#   `CodeConflict::NotAccepting` and the bound of five seconds on writing a code: the tool's input is there for as
#     long as the sign-in runs, and is gone only with its end, which no code reaches (the Python Host's own check of a
#     closed input is as unreachable there). The words are held by the vectors.
#   a sign that is cut in two at the end of a read waiting for its rest (`pass_on`): the stand-ins print what they
#     print in one write; the vectors hold what is shown of the whole text.
#   the five seconds a tool that was stopped, and the stream it held, are waited for: no stand-in outlives its stop.
#   the 503 when the desk is gone: its task ends only with the Host.
#   that the last line which is not empty says why (`.rfind(|line| !line.is_empty())`): what is shown is without blanks
#     at its ends, and every end of a line is a blank, so its last line is never empty. The Python Host's filter is
#     kept for its form.
#   that an address ends at an escape: what an app is shown has none left by then.
#   that blanks at the ends are Python's and not Rust's: the signs only Python counts as blanks are below the space
#     and taken out before.
#   that what a tool prints after the first read is told: when the stand-in's writes reach the Host in one read or
#     in several is the machine's to say, and a rule that holds only on the second would be caught by chance.
SECRET_SK = r'        r"[sS\x{{17f}}][kK\x{{212a}}]-[{letters}-]{{12,}}|[bB][eE][aA][rR][eE][rR]\s+[{letters}.-]{{12,}}"'
LETTERS = r'    let letters = r"a-zA-Z0-9_\x{130}\x{131}\x{17f}\x{212a}";'
PROMPT = r'    r"(?:[pP][aA][sS\x{17f}][tT][eE]|[eE][nN][tT][eE][rR])[^\n]*[cC][oO][dD][eE][^\n]*>[ \t]*";'
FILTER = "        .filter(|sign| matches!(sign, '\\n' | '\\t') || u32::from(*sign) >= 32)"
LAST = '    let last = match text.char_indices().nth(count.saturating_sub(SHOWN)) {'
FITS = "        let fits = candidate[..4].iter().all(part)\n            && candidate[4] == '-'"
CAN_GO = '    let Some(told) = told.filter(|told| told["status"] == "running" && tool_runs) else {'
START_RUNNING = '                if let Some(entry) = entries.get_mut(&tool).filter(|entry| entry.running()) {'
PLANTS = [
    # What an app is shown of what a tool printed.
    (K, 'vectors', 'a title closed by a bell is kept', r'static OSC: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)\x1b\].*?(?:\x07|\x1b\\)"));', r'static OSC: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)\x1b\].*?(?:\x1b\\)"));'),
    (K, 'vectors', 'a title takes everything to its last close', r'regex(r"(?s)\x1b\].*?(?:\x07|\x1b\\)")', r'regex(r"(?s)\x1b\].*(?:\x07|\x1b\\)")'),
    (K, 'vectors', 'a title left open takes only its line', r'regex(r"(?s)\x1b\].*\z")', r'regex(r"\x1b\].*$")'),
    (K, 'vectors', 'a title left open is kept', '    let text = OSC_OPEN.replace_all(&text, "");\n', ''),
    (K, 'vectors', 'a colour with parameters between is kept', r'regex(r"\x1b\[[0-?]*[ -/]*[@-~]")', r'regex(r"\x1b\[[0-9]*[@-~]")'),
    (K, 'vectors', 'a colour without its end is kept', '    let text = SINGLE.replace_all(&text, "");\n', ''),
    (K, 'vectors', 'a single escape takes a small letter too', r'regex(r"\x1b[@-_]")', r'regex(r"\x1b[@-z]")'),
    (K, 'vectors', 'the tab is taken out', FILTER, "        .filter(|sign| matches!(sign, '\\n') || u32::from(*sign) >= 32)"),
    (K, 'vectors', 'the line feed is taken out', FILTER, "        .filter(|sign| matches!(sign, '\\t') || u32::from(*sign) >= 32)"),
    (K, 'vectors', 'the sign of deletion is taken out too', FILTER, "        .filter(|sign| matches!(sign, '\\n' | '\\t') || (32..127).contains(&u32::from(*sign)))"),
    (K, 'vectors', 'a return is kept', FILTER, "        .filter(|sign| matches!(sign, '\\n' | '\\t' | '\\r') || u32::from(*sign) >= 32)"),
    (K, 'vectors', 'a key of eleven signs is hidden', SECRET_SK, SECRET_SK.replace('[{letters}-]{{12,}}', '[{letters}-]{{11,}}')),
    (K, 'vectors', 'a key of twelve signs is shown', SECRET_SK, SECRET_SK.replace('[{letters}-]{{12,}}', '[{letters}-]{{13,}}')),
    (K, 'vectors', 'a key in capitals is shown', SECRET_SK, SECRET_SK.replace(r'[sS\x{{17f}}][kK\x{{212a}}]', 'sk')),
    (K, 'vectors', 'a bearer in capitals is shown', SECRET_SK, SECRET_SK.replace('[bB][eE][aA][rR][eE][rR]', 'bearer')),
    (K, 'vectors', 'a bearer joined to its token is hidden', SECRET_SK, SECRET_SK.replace(r'\s+', r'\s*')),
    (K, 'vectors', 'a token with a point is cut at it', SECRET_SK, SECRET_SK.replace('[{letters}.-]', '[{letters}-]')),
    (K, 'vectors', "Python's four letters beyond ASCII are not letters of a key", LETTERS, '    let letters = r"a-zA-Z0-9_";'),
    (K, 'vectors', 'what looks like a key is shown', '    let text = SECRET.replace_all(&text, "[redacted]");', '    let text = std::borrow::Cow::<str>::Owned(text);'),
    (K, 'vectors', 'a sign more is shown', LAST, LAST.replace('count.saturating_sub(SHOWN)', 'count.saturating_sub(SHOWN + 1)').replace('Some((at, _)) if', 'Some((at, _)) if')),
    (K, 'vectors', 'the first of what was printed is shown', LAST, '    let last = match text.char_indices().nth(0) {'),
    (K, 'vectors', 'blanks at the ends are kept', '    python::strip(last).to_string()\n}', '    last.to_string()\n}'),
    (K, 'vectors', 'less is kept of what was printed', '        && let Some((at, _)) = kept.char_indices().nth(count - KEPT)', '        && let Some((at, _)) = kept.char_indices().nth(count - KEPT + 1)'),
    (K, 'vectors', 'everything printed is kept', '        kept.drain(..at);', '        let _ = at;'),
    # Why it failed.
    (K, 'vectors', 'the first line is why', '        .rfind(|line| !line.is_empty());', '        .find(|line| !line.is_empty());'),
    (K, 'vectors', 'the prompt before a failure is kept', '        PROMPT_AT_START.replace(line, "").into_owned()', '        line.to_string()'),
    (K, 'vectors', 'nothing is said when nothing was printed', '        "Sign-in failed"\n', '        ""\n'),
    (K, 'vectors', 'why is a sign longer', '    python::first(summary, 500).to_string()', '    python::first(summary, 501).to_string()'),
    (K, 'vectors', 'why is measured in bytes', '    python::first(summary, 500).to_string()', '    summary.get(..500).unwrap_or(summary).to_string()'),
    # The address, the code and whether a code is asked for.
    (K, 'vectors', 'any address is the one to open', '        .find(|url| url.contains("auth") || url.contains("login"))', '        .find(|_| true)'),
    (K, 'vectors', 'an address about logging in is passed over', '        .find(|url| url.contains("auth") || url.contains("login"))', '        .find(|url| url.contains("auth"))'),
    (K, 'vectors', 'the stops after an address are kept', ".map_or(\"\", |url| url.trim_end_matches(['.', ',', ')']));", '.map_or("", |url| url);'),
    (K, 'vectors', 'a bracket after an address is kept', ".map_or(\"\", |url| url.trim_end_matches(['.', ',', ')']));", ".map_or(\"\", |url| url.trim_end_matches(['.', ',']));"),
    (K, 'vectors', 'a code in small letters is a code', '    let part = |sign: &char| sign.is_ascii_uppercase() || sign.is_ascii_digit();', '    let part = |sign: &char| sign.is_ascii_alphanumeric();'),
    (K, 'vectors', 'a code may follow a capital', '            && !before.is_some_and(part)\n', '\n'),
    (K, 'vectors', 'a code may be followed by a capital', '            && !signs.get(at + 10).is_some_and(part);', ';'),
    (K, 'vectors', 'a code needs no dash', FITS, "        let fits = candidate[..4].iter().all(part)\n            && candidate[4] != ' '"),
    (K, 'vectors', 'a code at the very end is not found', '    (0..signs.len().saturating_sub(9)).find_map(|at| {', '    (0..signs.len().saturating_sub(10)).find_map(|at| {'),
    (K, 'vectors', 'the code and the address are good for a minute less', '        started + TIMEOUT_MS\n', '        started + TIMEOUT_MS - 60_000\n'),
    (K, 'vectors', 'only an address says how long it is good', '    let expires_at = if code.is_some() || !url.is_empty() {', '    let expires_at = if !url.is_empty() {'),
    (K, 'vectors', 'a sign-in started at no time starts at 0', '    let started = if started_at == 0 { now_ms } else { started_at };', '    let started = started_at;'),
    (K, 'vectors', 'a sign-in that ended still asks for a code', '    let waiting = state == State::Running && PROMPT_AT_END.is_match(&clean);', '    let waiting = PROMPT_AT_END.is_match(&clean);'),
    (K, 'vectors', 'a prompt anywhere asks for a code', r'LazyLock::new(|| regex(&format!(r"{PROMPT}\z")));', r'LazyLock::new(|| regex(&format!(r"{PROMPT}")));'),
    (K, 'vectors', 'a prompt is only asked for in small letters', PROMPT, r'    r"(?:paste|enter)[^\n]*code[^\n]*>[ \t]*";'),
    (K, 'vectors', 'a prompt to enter is no prompt', PROMPT, r'    r"(?:[pP][aA][sS\x{17f}][tT][eE])[^\n]*[cC][oO][dD][eE][^\n]*>[ \t]*";'),
    (K, 'vectors', 'a prompt answered still asks', '        "awaiting_code": waiting && clean != answered,', '        "awaiting_code": waiting,'),
    (K, 'vectors', 'a code is said to be answered without a prompt', '        "code_submitted": waiting && clean == answered,', '        "code_submitted": clean == answered,'),
    # A code pasted back.
    (K, 'vectors', 'a code is taken with its blanks', '    let code = python::strip(said);', '    let code = said;'),
    (K, 'vectors', 'a code may be a sign longer', '    if code.chars().count() > LONGEST_CODE', '    if code.chars().count() > LONGEST_CODE + 1'),
    (K, 'vectors', 'a code of the most signs is too long', '    if code.chars().count() > LONGEST_CODE', '    if code.chars().count() >= LONGEST_CODE'),
    (K, 'vectors', 'a code is measured in bytes', '    if code.chars().count() > LONGEST_CODE', '    if code.len() > LONGEST_CODE'),
    (K, 'vectors', 'a code of two lines is taken', "|| code.contains(['\\n', '\\r'])", "|| code.contains('\\r')"),
    (K, 'vectors', 'a code with a return is taken', "|| code.contains(['\\n', '\\r'])", "|| code.contains('\\n')"),
    (K, 'vectors', 'a code goes to a sign-in that ended', CAN_GO, '    let Some(told) = told.filter(|_| tool_runs) else {'),
    (K, 'vectors', 'a code goes to a sign-in with no tool', CAN_GO, '    let Some(told) = told.filter(|told| told["status"] == "running") else {'),
    (K, 'vectors', 'a code goes to a sign-in that asks for none', '    if !python::truthy(&told["awaiting_code"]) {', '    if python::truthy(&told["code_submitted"]) {'),
    (K, 'vectors', 'a tool is named only in small letters', '    match python::strip(tool).to_lowercase().as_str() {', '    match python::strip(tool) {'),
    (K, 'vectors', 'a tool is named only without blanks', '    match python::strip(tool).to_lowercase().as_str() {', '    match tool.to_lowercase().as_str() {'),
    (K, 'vectors', 'Grok is shown to every app', '            listed_by_default: false,', '            listed_by_default: true,'),
    (K, 'vectors', "Codex is signed in with a browser", '            login: &["login", "--device-auth"],\n            logout: &["logout"],\n            listed_by_default: true,', '            login: &["login"],\n            logout: &["logout"],\n            listed_by_default: true,'),
    (K, 'vectors', "Claude Code signs in to the console", '            login: &["auth", "login", "--claudeai"],', '            login: &["auth", "login", "--console"],'),
    (K, 'vectors', 'a tool is named in capitals', '    python::strip(&field(body.get("backend"))).to_lowercase()', '    python::strip(&field(body.get("backend"))).to_string()'),
    (K, 'vectors', 'a field that is no text is nothing', '            other => python::repr(other),', '            _ => String::new(),'),
    # The Host: one keeps the tools.
    (H, 'host', 'a Host beside the one that keeps the tools signs them in', '    app.config.accounts == Accounts::Select\n', '    app.config.accounts != Accounts::None\n'),
    (H, 'host', 'a Host with no mode signs the tools in', '    app.config.accounts == Accounts::Select\n', '    app.config.accounts != Accounts::Follow\n'),
    (H, 'host', 'a Host that does not keep the tools is not asked', '    if !keeps(&app) {\n        return ApiError::new(StatusCode::CONFLICT, NOT_KEPT).into_response();\n    }\n', ''),
    (H, 'host', 'the refusal is not built', '        return ApiError::new(StatusCode::CONFLICT, NOT_KEPT).into_response();', '        return ApiError::new(StatusCode::NOT_IMPLEMENTED, NOT_KEPT).into_response();'),
    (H, 'host', 'a request is read before the refusal', '    if !keeps(&app) {\n        return ApiError::new(StatusCode::CONFLICT, NOT_KEPT).into_response();\n    }\n    let said = match serde_json::from_slice::<Value>(&body) {\n        _ if body.is_empty() => Map::new(),\n        Ok(Value::Object(said)) => said,\n        _ => return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response(),\n    };', '    let said = match serde_json::from_slice::<Value>(&body) {\n        _ if body.is_empty() => Map::new(),\n        Ok(Value::Object(said)) => said,\n        _ => return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response(),\n    };\n    if !keeps(&app) {\n        return ApiError::new(StatusCode::CONFLICT, NOT_KEPT).into_response();\n    }'),
    (H, 'host', 'every way under the door is a change', '    if !matches!(action, "login" | "login-code" | "logout") {', '    if action.is_empty() {'),
    (H, 'host', 'a list is a request', '        Ok(Value::Object(said)) => said,', '        Ok(Value::Object(said)) => said,\n        Ok(_) => Map::new(),'),
    ('clarp-host/src/models.rs', 'host', 'the Host that keeps the tools says it cannot sign them in', '                row["supports_auth"] = json!(true);', '                row["supports_auth"] = json!(false);'),
    ('clarp-host/src/models.rs', 'host', 'every Host says it signs the tools in', '    if crate::logins::sign_in::keeps(&app) {\n        for tool', '    if true {\n        for tool'),
    (E, 'host', 'every Host lists backend_auth', '            && (*feature != "backend_auth" || crate::logins::sign_in::keeps(app))', ''),
    (H, 'host', 'a row says nothing of a sign-in from here', '    if every {\n        row["can_login"] = json!(true);', '    if !every {\n        row["can_login"] = json!(true);'),
    (H, 'host', 'the note to sign in on the machine stays', '        row["note"] = json!("");', '        let _ = &row["note"];'),
    (H, 'host', 'a login cannot be signed out', '        row["can_logout"] = row["credential_present"].clone();', '        row["can_logout"] = json!(false);'),
    (H, 'host', 'a missing login can be signed out', '        row["can_logout"] = row["credential_present"].clone();', '        row["can_logout"] = json!(true);'),
    (H, 'host', 'every tool can be signed in from here', '    if sign_in::driver(tool).is_none() {\n        return;\n    }\n    if every', '    if every'),
    (H, 'host', "an older app is told Grok's sign-in", '                .filter(|tool| sign_in::driver(tool).is_some())', '                .chain(std::iter::once("grok"))'),
    # Signing in.
    (H, 'host', 'a sign-in is answered 200', '            .map(|told| (StatusCode::ACCEPTED, Json(told)).into_response()),', '            .map(|told| Json(told).into_response()),'),
    (H, 'host', 'a tool that is not on the machine is signed in', '    if !crate::logins::installed(&program) {\n        return Err(refused());\n    }\n', ''),
    (H, 'host', 'a second sign-in starts while one runs', START_RUNNING, '                if let Some(entry) = entries.get_mut(&tool).filter(|_| false) {'),
    (H, 'host', "the tool's errors are not read", '            .stderr(writer)', '            .stderr(std::process::Stdio::null())'),
    (H, 'host', 'a sign-in that ran too long is not said to have', '                let more = "\\nLogin timed out.".to_string();', '                let more = String::new();'),
    (H, 'host', 'a sign-in is never stopped', '                let _ = child.start_kill();', '                let _ = child.wait().await;'),
    (H, 'host', 'a sign-in that failed completed', '                        Ending::Exited { well: false } => {\n                            (Stage::Failed, sign_in::failure(&entry.printed))', '                        Ending::Exited { well: false } => {\n                            (Stage::Complete, sign_in::failure(&entry.printed))'),
    (H, 'host', 'a sign-in that failed says not why', '                        Ending::Exited { well: false } => {\n                            (Stage::Failed, sign_in::failure(&entry.printed))', '                        Ending::Exited { well: false } => {\n                            (Stage::Failed, String::new())'),
    (H, 'host', 'a completed sign-in says it failed', '                        Ending::Exited { well: true } => (Stage::Complete, String::new()),', '                        Ending::Exited { well: true } => (Stage::Complete, sign_in::failure(&entry.printed)),'),
    # A code.
    (H, 'host', 'a code goes without its line end', '    let line = format!("{code}\\n");', '    let line = code.to_string();'),
    (H, 'host', 'a code answered is asked again', '        "",\n        &shown,\n    );', '        "",\n        "",\n    );'),
    (H, 'host', 'a code is refused as no request', '    answer.map_err(|conflict| ApiError::new(StatusCode::CONFLICT, conflict.words()))', '    answer.map_err(|conflict| ApiError::new(StatusCode::BAD_REQUEST, conflict.words()))'),
    (H, 'host', 'a code for a tool not signed in from here goes on', '    if sign_in::driver(tool).is_none() {\n        return Err(ApiError::new(', '    if sign_in::driver(tool).is_some_and(|_| false) {\n        return Err(ApiError::new('),
    # Signing out.
    (H, 'host', 'a sign-out the tool refused is done', '    if !done.status.success() {', '    if done.status.code() == Some(2) {'),
    (H, 'host', "a refused sign-out says the tool's words as they are", '    let conflict = |why: &str| ApiError::new(StatusCode::CONFLICT, sign_in::failure(why));', '    let conflict = |why: &str| ApiError::new(StatusCode::CONFLICT, why.trim().to_string());'),
    (H, 'host', 'a sign-out keeps the sign-in', '    let _ = app.sign_ins.orders(&app.clock).send(Order::Forget {', '    let _ = app.sign_ins.orders(&app.clock).send(Order::Printed { run: 0, more: String::new(),'),
    (H, 'host', 'a sign-out is answered with the row an older app is told', '    let told = rows(app, true, false).await?;', '    let told = rows(app, false, false).await?;'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
