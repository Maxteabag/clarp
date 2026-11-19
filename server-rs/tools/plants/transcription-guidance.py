#!/usr/bin/env python3
"""Wrong rules for saved guidance, preview bounds and the shared engine roster."""
import runner

RUNS = {'kernel':['test','--test','transcription_guidance_vectors'],
        'host':['test','--test','host','--','transcription_guidance_reaches_the_phone']}
K='clarp-kernel/src/vocabulary/guidance.rs'
H='clarp-host/src/heard/guidance.rs'
C='clarp-host/src/vocabulary/compile.rs'
PLANTS=[]


def change(file, run, pairs):
    for old,new in pairs:
        PLANTS.append((file,run,f'{old} becomes {new}',old,new))


change(K,'kernel',[
    ('value.unwrap_or("true")','value.unwrap_or("false")'),
    ('python::strip(value.unwrap_or("true"))','value.unwrap_or("true")'),
    ('.to_ascii_lowercase()', '.to_string()'),
    ('"1" | "true" | "yes" | "on"', '"true" | "yes" | "on"'),
    ('"1" | "true" | "yes" | "on"', '"1" | "yes" | "on"'),
    ('"1" | "true" | "yes" | "on"', '"1" | "true" | "on"'),
    ('"1" | "true" | "yes" | "on"', '"1" | "true" | "yes"'),
    ('python::splitlines(raw)','raw.lines().collect::<Vec<_>>()'),
    ("line.split('#')","line.split('~')"),
    ('.unwrap_or("").trim_matches(TRIM)', '.unwrap_or("").trim()'),
    ('.filter(|line| !line.is_empty())','.filter(|_| true)'),
    ('.join("\\n")','.join(" ")'),
    ('first(&lines, 2000)','first(&lines, 1999)'),
    ('first(&lines, 2000)','first(&lines, 2001)'),
    ('.trim_end_matches(python::blank)', '.trim_end_matches(\'~\')'),
    ('!a.archived && !a.deleted','!a.deleted'),
    ('!a.deleted && !a.janitor','!a.janitor'),
    ('!a.deleted && !a.janitor','!a.deleted'),
    ('sort_by_key(|a| a.created)','sort_by_key(|a| std::cmp::Reverse(a.created))'),
    ('seen.insert(python::casefold(s))','seen.insert(s.to_string())'),
    ('!s.is_empty() && seen.insert','seen.insert'),
    ('None => enabled','None => !enabled'),
    ('"delegation_agent_names_enabled must be boolean"','"invalid switch"'),
    ('None => clean(stored)','None => String::new()'),
    ('Some(Value::String(s)) => clean(s)','Some(Value::String(s)) => s.clone()'),
    ('Some(v) if python::truthy(v)','Some(v) if !python::truthy(v)'),
    ('Some(_) => String::new()', 'Some(_) => "null".into()'),
    ('names.iter().rev()','names.iter()'),
    ('format!("{name}, {kept}")','format!("{kept}, {name}")'),
    ('prompt.chars().count() > CHAR_LIMIT','prompt.chars().count() >= CHAR_LIMIT'),
    ('estimate_tokens(&prompt) > TOKEN_LIMIT','estimate_tokens(&prompt) >= TOKEN_LIMIT'),
    ('CHAR_LIMIT: usize = 600','CHAR_LIMIT: usize = 601'),
    ('TOKEN_LIMIT: usize = 223','TOKEN_LIMIT: usize = 224'),
    ('"Technical vocabulary: "', '"Technical terms: "'),
    ('let glossary = python::splitlines(&clean(glossary)).join(" ");','let glossary = clean(glossary);'),
    ('CHAR_LIMIT - prefix.len() - 2','CHAR_LIMIT - prefix.len() - 1'),
    ('estimate_tokens(&prompt) <= TOKEN_LIMIT','estimate_tokens(&prompt) < TOKEN_LIMIT'),
    ('saturating_sub(8)','saturating_sub(1)'),
    ('let delegation = if enabled', 'let delegation = if !enabled'),
    ('"delegation_agent_names_enabled":true','"delegation_agent_names_enabled":false'),
    ('"technical_glossary":""','"technical_glossary":"words"'),
    ('"active_agent_names":names','"active_agent_names":[]'),
    ('"regular_estimated_prompt_tokens":estimate_tokens(&regular)','"regular_estimated_prompt_tokens":0'),
    ('"delegation_estimated_prompt_tokens":estimate_tokens(&delegation)','"delegation_estimated_prompt_tokens":0'),
])
change(H,'host',[
    ('archived: r.archived_at_ms.is_some()', 'archived: false'),
    ('directory.setting(GLOSSARY_KEY).unwrap_or(""),\n        &active_names(directory)', 'directory.setting(NAMES_KEY).unwrap_or(""),\n        &active_names(directory)'), # narrow the read below
    ('(GLOSSARY_KEY.into(), glossary)', '(GLOSSARY_KEY.into(), String::new())'),
    ('(NAMES_KEY.into(), enabled.to_string())','(NAMES_KEY.into(), (!enabled).to_string())'),
    ('out["ok"] = json!(true)','out["ok"] = json!(false)'),
])
change(C,'host',[
    ('if delegated && guidance::names_enabled', 'if !delegated && guidance::names_enabled'),
    ('crate::heard::guidance::active_names(&dir)','Vec::new()'),
    ('glossary: if delegated','glossary: if !delegated'),
])
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
