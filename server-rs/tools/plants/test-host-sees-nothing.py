#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A test Host sees nothing of the machine: the rule (`clarp-sealed`) that builds the environment of every process the
Host's and the shim's tests start from nothing, its home and folders inside the test's own place, its constants, every
program a person installs pinned to a path that is not there from the product's own table, and nothing of the
runner's but what a test names; and the tests that hold it: the rule as a function, what a process really sees, a
Host on a machine that has everything, the installed Host's file, the tools a Host knows, the scan of what the Host's
crates read, and the scan of the starts of commands at a fixed address.
"""
import runner

RUNS = {
    'sealed': ['test', '-p', 'clarp-host', '--test', 'host', '--', 'harness::'],
    'shim': ['test', '-p', 'clarp-shim', '--test', 'shim'],
    'table': ['test', '-p', 'clarp-backend', '--lib', '--', 'every_program_of_the_table'],
}
S = 'clarp-sealed/src/lib.rs'
H = 'clarp-host/tests/host.rs'
P = 'clarp-host/tests/host/sealed.rs'
B = 'clarp-backend/src/lib.rs'
# NOT PLANTED, each with its reason:
#   that what a test names of the runner is taken whole for every process of that test: the three tests that name
#     any are the ignored ones that run a real tool, and a wrong rule there would run a real tool.
#   that the shim's tests give the shim the rule (`shim_in`): the rule's own command is planted below (`env_clear`
#     dropped), and the shim's test of what its tool sees catches it there.


def of_the_runner(name, default):
    return (f'runner.iter().find(|(own, _)| own == "{name}").map_or_else(|| {default}, |(_, v)| v.clone())')


PINNED = '        set(variable, absent(place).join(name).into());'
PINNED_BY_THE_RUNNER = ('        set(variable, runner.iter().find(|(own, _)| own == *variable)'
                        '.map_or_else(|| absent(place).join(name).into(), |(_, v)| v.clone()));')
GIVEN = '    given\n}\n\n/// A command whose whole environment'
KEYS_TOO = ('    for (own, value) in runner {\n'
            '        if own.to_string_lossy().ends_with("_KEY") {\n'
            '            given.insert(own.clone(), value.clone());\n'
            '        }\n'
            '    }\n' + GIVEN)
PLANTS = [
    # Of the runner's own, nothing.
    (S, 'sealed', "the default lets the runner's home through", '    set("HOME", home.clone().into());',
     f'    set("HOME", {of_the_runner("HOME", "home.clone().into()")});'),
    (S, 'sealed', "the runner's data folder is the process's", '    set("XDG_DATA_HOME", home.join(".local/share").into());',
     f'    set("XDG_DATA_HOME", {of_the_runner("XDG_DATA_HOME", "home.join(\".local/share\").into()")});'),
    (S, 'sealed', "the runner's folder of settings is the process's", '    set("XDG_CONFIG_HOME", home.join(".config").into());',
     f'    set("XDG_CONFIG_HOME", {of_the_runner("XDG_CONFIG_HOME", "home.join(\".config\").into()")});'),
    (S, 'sealed', "the runner's cache is the process's", '    set("XDG_CACHE_HOME", home.join(".cache").into());',
     f'    set("XDG_CACHE_HOME", {of_the_runner("XDG_CACHE_HOME", "home.join(\".cache\").into()")});'),
    (S, 'sealed', "the runner's state is the process's", '    set("XDG_STATE_HOME", home.join(".local/state").into());',
     f'    set("XDG_STATE_HOME", {of_the_runner("XDG_STATE_HOME", "home.join(\".local/state\").into()")});'),
    (S, 'sealed', "the runner's runtime folder is the process's", '    set("XDG_RUNTIME_DIR", place.join("user-run").into());',
     f'    set("XDG_RUNTIME_DIR", {of_the_runner("XDG_RUNTIME_DIR", "place.join(\"user-run\").into()")});'),
    (S, 'sealed', "the runner's scratch folder is the process's", '    set("TMPDIR", place.join("tmp").into());',
     f'    set("TMPDIR", {of_the_runner("TMPDIR", "place.join(\"tmp\").into()")});'),
    (S, 'sealed', "the runner's path is the process's", '    set("PATH", PATH.into());',
     f'    set("PATH", {of_the_runner("PATH", "PATH.into()")});'),
    (S, 'sealed', "the runner's language is the process's", '    set("LANG", LANG.into());',
     f'    set("LANG", {of_the_runner("LANG", "LANG.into()")});'),
    (S, 'sealed', 'no time zone is given', '    set("TZ", TZ.into());\n', ''),
    (S, 'sealed', "a key of the runner's reaches the process", GIVEN, KEYS_TOO),
    # No tool of the machine's.
    (S, 'sealed', 'the default lets a tool the runner names be found', PINNED, PINNED_BY_THE_RUNNER),
    (S, 'sealed', 'no program is pinned', '    for (variable, name) in programs {', '    for (variable, name) in programs.iter().take(0) {'),
    (P, 'sealed', "a program the Host starts of its own is not pinned", '    all.push((variable, name));', '    let _ = (variable, name);'),
    (B, 'sealed', 'a tool the Host knows is left out of the table', '    ("muse", "CLARP_MUSE_PROGRAM", "muse"),\n', ''),
    (B, 'table', "a tool's variable is not a program's", '    ("grok", "CLARP_GROK_PROGRAM", "grok"),', '    ("grok", "CLARP_GROK_BINARY", "grok"),'),
    (B, 'table', "a tool's name on the path is a path", '    ("codex", CODEX_PROGRAM_ENV, "codex"),', '    ("codex", CODEX_PROGRAM_ENV, "/usr/bin/codex"),'),
    # What a test names on purpose, and only that.
    (S, 'sealed', 'what a test names of the runner is not taken', '            Some((_, value)) => given.insert(OsString::from(name), value.clone()),', '            Some(_) => None,'),
    (S, 'sealed', "a name the runner lacks keeps the default's", '            None => given.remove(OsStr::new(name)),', '            None => None,'),
    # The command inherits nothing.
    (S, 'sealed', 'a command inherits the runner\'s environment under the rule', '    command.env_clear().envs(given);', '    command.envs(given);'),
    (S, 'shim', "a shim's command inherits the runner's environment under the rule", '    command.env_clear().envs(given);', '    command.envs(given);'),
    (H, 'sealed', "the Host's tests start their processes past the rule", '    clarp_sealed::command(program, sealed::sealed(runner, root, named))',
     '    let _ = (runner, root, named);\n    Command::new(program)'),
    # What the Host's crates read is decided on.
    (S, 'sealed', 'a variable the Host reads is decided on nowhere', '    ("CODEX_HOME", "home"),\n', ''),
    (S, 'sealed', 'a name nobody reads is listed', '    "AGY_OK",\n', '    "AGY_OK",\n    "NEVER_READ_ANYWHERE",\n'),
    (H, 'sealed', 'a new integration fixture bypasses the shared process constructor',
     'let mut second = place.command("sleep").arg("60").spawn().unwrap();',
     'let mut second = Command::new("sleep").arg("60").spawn().unwrap();'),
    # A command at a fixed address names the Host it speaks to.
    (H, 'sealed', "a test asks a Host's relay status at the fixed address", '        .args(["network", "relay", "status", "--host", &host.address])',
     '        .args(["network", "relay", "status"])'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
