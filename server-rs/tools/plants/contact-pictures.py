#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The picture of a contact: whether its job is begun and as which try (the kernel's admission, held to the Python Host's
enqueue_computer_capped), what the provider is asked for it, and the Host's own task that makes it (the word of the
installation, the guards of its try, what is kept and said, a Host that stopped in the middle).
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--test', 'contact_vectors'],
    'host': ['test', '--test', 'host', '--', 'pictures::'],
}
K = 'clarp-kernel/src/contacts.rs'
P = 'clarp-host/src/pictures.rs'
F = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   pictures.rs, the guard before the provider is asked: the job is begun and started in one go, so no test can end it
#     between the two without a race; the guards of the keeping (the end waited for, the generation) are planted.
#   pictures.rs `under_way` without the installation's word: without it no job is ever begun, so nothing is under way.
#   pictures.rs the lock of the admission and the registration: one contact's picture is asked twice at once only by
#     launches at once of one contact, which the fleet refuses as occupied; nothing can show the lock gone.
PLANTS = [
    # ---- whether a picture's job is begun, and as which try
    (K, 'kernel', 'a picture that was made is made again', '                "queued" | "running" | "succeeded" => return None,',
     '                "queued" | "running" => return None,'),
    (K, 'kernel', 'a fourth try is begun', '            if tried.generation >= u64::from(MAX_ATTEMPTS) {',
     '            if tried.generation > u64::from(MAX_ATTEMPTS) {'),
    (K, 'kernel', 'a failed try is begun again without its wait', '            if tried.ended_ago_ms.is_some_and(|ago| ago < wait) {',
     '            if tried.ended_ago_ms.is_some_and(|ago| ago < 0) {'),
    (K, 'kernel', 'every try waits as the first does', '            let wait = RETRY_BACKOFF_MS[at.min(RETRY_BACKOFF_MS.len() - 1)];',
     '            let wait = RETRY_BACKOFF_MS[0];'),
    (K, 'kernel', 'a twenty-first picture is begun in a day', '        if begun_today >= DAILY_LIMIT {', '        if begun_today > DAILY_LIMIT {'),
    (K, 'kernel', 'every try is the first', '        Some(before.map_or(1, |tried| tried.generation + 1))', '        Some(1)'),
    # ---- what the provider is asked
    (K, 'kernel', 'every contact is asked for one kind of face',
     '        spec.portrait_varieties[index % spec.portrait_varieties.len()].as_str()', '        spec.portrait_varieties[0].as_str()'),
    (K, 'kernel', "a contact of no tier is drawn as Claude's whatever its tool",
     '        tier_of_tool(backend).to_string()', '        "claude".to_string()'),
    # ---- the Host's own task
    (P, 'host', 'a picture is drawn without the installation\'s word',
     '    if !app.config.contact_pictures {\n        return;\n    }\n    let persona = {', '    let persona = {'),
    (P, 'host', 'the owner\'s key is kept in why a picture failed', '            said = said.replace(secret, put);', '            said = said.replace(secret, secret);'),
    # Two guards keep a cancelled try's picture out, each enough alone: the end of the try, waited for beside the work,
    # drops the work; the generation looked at before the keeping refuses it. The wrong rule takes both away.
    (P, 'host', 'a try that was cancelled keeps its picture all the same',
     '        if !current(app, id, generation).await {\n            return Err(None);\n        }\n        app.fleet\n'
     '            .picture_made(persona_id.to_string(), picture)\n            .await\n            .map_err(|why| Some(why.0))\n'
     '    };\n    // The work beside the end of its try: a cancel, a finish or a new try ends the waiting,\n'
     '    // and the work with it (what the provider still answers is not read).\n    let made = tokio::select! {\n'
     '        made = work => made,\n        _ = app.fleet.wait_computer_job_end(id.to_string(), generation) => Err(None),\n    };\n',
     '        app.fleet\n'
     '            .picture_made(persona_id.to_string(), picture)\n            .await\n            .map_err(|why| Some(why.0))\n'
     '    };\n    let made = work.await;\n'),
    (P, 'host', 'a picture a stopped Host left is ended as made', '            failed: Some(INTERRUPTED.to_string()),', '            failed: None,'),
    (F, 'host', 'the agents of a contact do not wear its picture', '            record.avatar.clone_from(&digest);\n', ''),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
