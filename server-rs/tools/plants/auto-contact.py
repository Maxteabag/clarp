#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Who an agent is when nobody named it (feature `auto_contact_minting`, contract 85): its rule in the kernel (the tier
of a tool and the tool of a tier, which contacts a tool's pool holds, the name and the way of speaking a minted
contact is given, which contact an agent that is made takes, what `POST /agent-assign` does), held to the Python
Host's own functions; and in the Host (`POST /agents` with `auto_contact` or `mint_contact`, `POST /agent-assign`,
the contact a running agent takes on, what the apps are told).
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--test', 'contact_vectors'],
    'host': ['test', '--test', 'host', '--', 'contacts::'],
}
C = 'clarp-kernel/src/contacts.rs'
H = 'clarp-host/src/contacts.rs'
E = 'clarp-host/src/edge.rs'
F = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   contacts.rs `given_name`, that the names of the roster's tiers are taken: no minted name is one of them (the two
#     lists share none), so no pick can show it; it stays as the Python Host has it.
#   contacts.rs `launch` for a janitor: a janitor is not made by `POST /agents` here, so nothing asks it.
#   fleet.rs `Amend::Contact`, `{}` for a persona with no voice: every persona is given one when it is made, here
#     as there, so no persona without one can be assigned.
PLANTS = [
    # ---- the tier of a tool, the tool of a tier
    (C, 'kernel', 'OpenCode is of its own tier', '        "codex" | "opencode" => "codex",', '        "codex" => "codex",'),
    (C, 'kernel', 'Antigravity is of Claude\'s tier', '        "gemini" | "agy" => "gemini",', '        "gemini" => "gemini",'),
    (C, 'kernel', 'a janitor\'s contacts run on no tool of their own', '        "codex" | "janitor" | "janitors" => "codex",',
     '        "codex" => "codex",'),
    (C, 'kernel', 'a contact\'s tool is compared with the case of its letters',
     '        Some(tool) => (folded(backend.trim()) == tool, Some(tool)),', '        Some(tool) => (backend == tool, Some(tool)),'),
    (C, 'kernel', 'a row\'s own tier is not read', '    let tier = if given.is_empty() {', '    let tier = if true {'),
    # ---- the name and the way of speaking of a minted contact
    (C, 'kernel', 'the first initial is skipped', '    const INITIALS: &str = "BCDEFGHJKLMNPQRSTVWXYZ";',
     '    const INITIALS: &str = "CDEFGHJKLMNPQRSTVWXYZ";'),
    (C, 'kernel', 'a name worn by an agent is free', '    let mut taken: HashSet<String> = occupied\n        .iter()\n        .chain(existing)',
     '    let mut taken: HashSet<String> = existing\n        .iter()\n        .chain(existing)'),
    (C, 'kernel', 'a number follows the names before the initials do',
     '    plain\n        .chain(initialed)\n        .chain(numbered)', '    plain\n        .chain(numbered)\n        .chain(initialed)'),
    (C, 'kernel', 'the way of speaking is read from the digest the other way round',
     '    let index = u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]]) as usize;\n    &spec.personalities',
     '    let index = u32::from_le_bytes([digest[0], digest[1], digest[2], digest[3]]) as usize;\n    &spec.personalities'),
    (C, 'kernel', 'the way of speaking is read from the name with its case',
     '    }\n    let digest = Sha256::digest(folded(name.trim()).as_bytes());',
     '    }\n    let digest = Sha256::digest(name.trim().as_bytes());'),
    # ---- the pool
    (C, 'kernel', 'a contact worn under another case of its letters is free',
     '        .filter(|contact| !occupied.contains(&folded(contact.name)))', '        .filter(|contact| !occupied.contains(contact.name))'),
    (C, 'kernel', 'a contact whose voice another agent speaks with is free',
     '                .is_none_or(|voice| !voices.contains(&voice))', '                .is_none_or(|_| true)'),
    (C, 'kernel', 'the agent asking does not count its own contact free',
     '        .filter(|agent| agent.agent_id != exclude)\n', ''),
    (C, 'kernel', 'the pool holds contacts of other tools', '        .filter(|contact| allows(contact.name, backend, contact.tier).0)',
     '        .filter(|_| true)'),
    # ---- which contact an agent that is made takes
    (C, 'kernel', 'any truth mints', '    let mint = asked.get("mint_contact") == Some(&Value::Bool(true));',
     '    let mint = truthy(asked.get("mint_contact"));'),
    (C, 'kernel', 'only `anonymous: true` goes against a contact', '    if truthy(asked.get("anonymous")) || truthy(asked.get("replace_sid")) || (mint && auto) {',
     '    if asked.get("anonymous") == Some(&Value::Bool(true)) || truthy(asked.get("replace_sid")) || (mint && auto) {'),
    (C, 'kernel', 'both a contact minted and one from the pool may be asked', '    if truthy(asked.get("anonymous")) || truthy(asked.get("replace_sid")) || (mint && auto) {',
     '    if truthy(asked.get("anonymous")) || truthy(asked.get("replace_sid")) {'),
    (C, 'kernel', '`mint_contact` takes the pool\'s first', '        Some(name) if !mint => Launch::Pooled(name.to_string()),',
     '        Some(name) => Launch::Pooled(name.to_string()),'),
    # ---- what `POST /agent-assign` does
    (C, 'kernel', 'an agent assigned automatically does not keep its own contact',
     '            .find(|choice| **choice == agent.persona)', '            .find(|_| false)'),
    (C, 'kernel', 'a contact is chosen by its name with the case of its letters',
     '                .find(|contact| folded(contact.name) == wanted)', '                .find(|contact| contact.name == name)'),
    (C, 'kernel', 'a contact another agent wears may be chosen', '            if occupied.contains(&folded(contact.name)) {', '            if false {'),
    (C, 'kernel', 'a name of sixty-one signs is made', '            if name.is_empty() || name.chars().count() > 60 {',
     '            if name.is_empty() || name.chars().count() > 61 {'),
    (C, 'kernel', 'a persona of that name is made again', '            if occupied.contains(&folded(&name)) || exists {',
     '            if occupied.contains(&folded(&name)) {'),
    (C, 'kernel', 'the contacts of an agent are not listed', 'pub const MODES: [&str; 4] = ["auto", "choose", "create", "options"];',
     'pub const MODES: [&str; 4] = ["auto", "choose", "create", "auto"];'),
    (C, 'kernel', 'a name is kept with its runs of white space',
     '    text.split_whitespace().collect::<Vec<_>>().join(" ")', '    text.trim().to_string()'),
    # ---- the Host
    (F, 'host', 'a minted contact is told to the apps as a persona made', '        let made = self.persona_write(None, &said, fetched, false)?;',
     '        let made = self.persona_write(None, &said, fetched, true)?;'),
    (F, 'host', 'a contact assigned is told as a new name', '        let renamed = record.persona != before.persona && !assigned;',
     '        let renamed = record.persona != before.persona;'),
    (F, 'host', 'an agent assigned a contact keeps its own way of speaking', '                record.personality = of_persona("personality");\n', ''),
    (F, 'host', 'an agent assigned a contact keeps its own voice', '                record.voice_id = match of_persona("voice_id") {',
     '                record.voice_id = match record.voice_id.clone() {'),
    (H, 'host', 'the apps are not told of a contact assigned', '    app.hub.publish(told, app.clock.now_ms());\n', ''),
    (F, 'host', 'a minted contact speaks as nobody', '            personality: clarp_kernel::contacts::personality(&name, tier).to_string(),\n', ''),
    (F, 'host', 'a minted contact keeps the session asked for',
     '            request.name = self.contact_of_launch(&backend, &contact)?;\n            request.session = None;\n',
     '            request.name = self.contact_of_launch(&backend, &contact)?;\n'),
    # ---- the single writer: a contact is chosen in the turn that registers or gives it
    (F, 'host', 'a launch reads the pool without the agents registered before it',
     'clarp_kernel::contacts::pool(&contacts, &wearers, backend, "")', 'clarp_kernel::contacts::pool(&contacts, &[], backend, "")'),
    (F, 'host', 'a mint takes no account of the names already taken',
     'clarp_kernel::contacts::given_name(backend, &occupied, &existing)', 'clarp_kernel::contacts::given_name(backend, &occupied, &[])'),
    (F, 'host', 'an assignment by the rule mints nothing in the fleet\'s turn',
     '            Ok(clarp_kernel::contacts::Assigned::Mint) => {\n                self.mint_contact(&record.backend, fetched)\n            }\n',
     '            Ok(clarp_kernel::contacts::Assigned::Mint) => Ok(None),\n'),
    (E, 'host', 'a contact started again in place may be asked for', '    if replaces.is_some()\n        && let Launch::Refused',
     '    if false\n        && let Launch::Refused'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
