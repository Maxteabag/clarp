#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

One rule for every stand-in address (`clarp_kernel::address::on_this_machine`): each variable's use of it, what the
rule says of an address (parsed as a client parses it, on this machine by its parsed host, its port named, a path and
a query only where the variable takes them, kept as it was rebuilt), and how the Host refuses another (the variable
named, the address never).
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--lib', 'address::'],
    'unit': ['test', '-p', 'clarp-host', '--lib', 'config::'],
    'host': ['test', '--test', 'host', '--', 'stand_ins::'],
}
A = 'clarp-kernel/src/address.rs'
C = 'clarp-host/src/config.rs'
# NOT PLANTED, each with its reason:
#   address.rs, an empty authority refused: every other address it refuses a client fails to parse as well, and the
#     ones a client reads (more slashes than two) name to it another host than their text, which may not stand in the
#     repository, not even in a test.
#   address.rs, an authority that ends at its colon: every stand-in names its port, so such an address is refused as
#     one without a port all the same.
#   address.rs, the parsed user name and password refused: an authority without `@` has none, and one with it is
#     refused before it is parsed.
#   address.rs, the parsed scheme looked at again: the scheme of the text is, and the parser keeps it.
PLANTS = [
    # ---- what the rule says of an address
    (A, 'kernel', 'user information is taken', "            || authority.contains('@')\n", ''),
    (A, 'kernel', 'a backslash is taken', "            || raw.contains('\\\\')\n", ''),
    (A, 'kernel', 'a control character is taken', '            || raw.chars().any(char::is_control)\n', ''),
    (A, 'kernel', 'another host than this machine is taken', '    if !address.loopback() {\n        return None;\n    }\n', ''),
    (A, 'kernel', 'another scheme than the variable\'s is taken', 'Address::parse_in(raw, &[rule.scheme])',
     'Address::parse_in(raw, &["http", "https", "ws", "wss"])'),
    (A, 'kernel', 'an address without its port is taken', '    if rule.port && port.is_none_or(|port| port == 0) {',
     '    if false {'),
    (A, 'kernel', 'port zero is taken', '    if rule.port && port.is_none_or(|port| port == 0) {',
     '    if rule.port && port.is_none() {'),
    (A, 'kernel', 'a path is taken where the variable takes none', '    if !rule.path && named_path {', '    if false {'),
    (A, 'kernel', 'a query is taken where the variable takes none', '    if !rule.query && uri.query().is_some() {',
     '    if false {'),
    (A, 'kernel', 'the address is kept as it was given', '    Some(built)\n}', '    Some(raw.to_string())\n}'),
    (A, 'kernel', 'a socket is kept as an address of the web', '    let mut built = format!("{}://{host}", rule.scheme);',
     '    let mut built = format!("http://{host}");'),
    (A, 'kernel', 'the port is not kept', '        built.push_str(&format!(":{port}"));\n', ''),
    (A, 'kernel', 'a path is added where none was named', '    if named_path {\n        built.push_str(',
     '    if true {\n        built.push_str('),
    # ---- how the Host refuses another
    (C, 'unit', 'the address is said with its refusal', '        format!("{variable}: {what}")',
     '        format!("{variable}: {what}: {given}")'),
    (C, 'unit', 'the Host keeps the address as it was given', '    on_this_machine(given, rule).ok_or_else(',
     '    on_this_machine(given, rule).map(|_| given.to_string()).ok_or_else('),
    (C, 'unit', 'no stand-in is refused as another', '    if given.is_empty() {\n        return Ok(String::new());\n    }\n',
     ''),
    (C, 'host', 'a socket is judged as an address of the web', '        let web = |variable: &str| here(variable, Local::WEB);',
     '        let web = |variable: &str| here(variable, Local::SOCKET);'),
    (C, 'host', 'CLARP_FCM_ENDPOINT takes a path', '                path: false,\n                ..Local::WEB', '                ..Local::WEB'),
    (C, 'host', 'CLARP_GEMINI_LIVE_ENDPOINT takes a query', '                query: false,\n                ..Local::SOCKET',
     '                ..Local::SOCKET'),
    # ---- each variable's use of the rule: the wrong rule reads the variable as it was given
]
for variable in ['CLARP_JUDGE_ENDPOINT', 'CLARP_SCRIBE_ENDPOINT', 'CLARP_DEEPGRAM_VOICE_ENDPOINT', 'CLARP_INK_ENDPOINT',
                 'CLARP_GOOGLE_VOICE_ENDPOINT', 'CLARP_ELEVEN_VOICE_ENDPOINT', 'CLARP_NOVA_ENDPOINT', 'CLARP_PICTURE_ENDPOINT',
                 'CLARP_ROUTER_ENDPOINT', 'CLARP_CARTESIA_ENDPOINT', 'CLARP_APNS_ENDPOINT', 'CLARP_GEMINI_ENDPOINT',
                 'CLARP_GOOGLE_TOKEN_ENDPOINT', 'CLARP_GOOGLE_SPEECH_ENDPOINT']:
    PLANTS.append((C, 'host', f'{variable} is not judged by the rule', f'web("{variable}")?',
                   f'Ok::<String, String>(std::env::var("{variable}").unwrap_or_default())?'))
for variable in ['CLARP_FLUX_ENDPOINT', 'CLARP_LIVE_ENDPOINT', 'CLARP_RELAY_DIAL']:
    PLANTS.append((C, 'host', f'{variable} is not judged by the rule', f'here("{variable}", Local::SOCKET)?',
                   f'Ok::<String, String>(std::env::var("{variable}").unwrap_or_default())?'))
for variable, rule in [('CLARP_GEMINI_LIVE_ENDPOINT', 'query: false,\n                ..Local::SOCKET'),
                       ('CLARP_FCM_ENDPOINT', 'path: false,\n                ..Local::WEB')]:
    PLANTS.append((C, 'host', f'{variable} is not judged by the rule',
                   f'here(\n            "{variable}",\n            Local {{\n                {rule}\n            }},\n        )?',
                   f'Ok::<String, String>(std::env::var("{variable}").unwrap_or_default())?'))


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
