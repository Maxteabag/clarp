#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A janitor's failure reaches its owner: the kernel's rule (`janitors/attention.rs`), held to the Python Host's
`janitor_attention` by `janitor_attention_vectors.rs`; and the Host's side (the book kept with its items, what
`GET /attention` lists, the badge, archive and discard by the item's id, the item opened by its id, the apps told),
held by the Host's test of the tool explainer that fails, fails again and recovers.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'janitor_attention_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_janitor_that_fails_again_and_again_reaches_its_owner'],
    'order': ['test', '-p', 'clarp-host', '--lib', 'a_change_of_the_book_is_shown_only_once_it_is_kept'],
}
K = 'clarp-kernel/src/janitors/attention.rs'
J = 'clarp-host/src/janitors.rs'
L = 'clarp-host/src/library.rs'
# NOT PLANTED, each with its reason:
#   that `pending` shows an item of another configuration: an item of an older configuration is closed in the same
#     write that sets the new one, so `pending` never meets an open one (the reference checks it all the same).
#   the item's id from its reference (sha256, forty signs): held by the vectors' ids and the kernel's own test.
PLANTS = [
    # The kernel's rule.
    (K, 'vectors', 'one failure raises an item', 'const RAISED_AT: usize = 2;', 'const RAISED_AT: usize = 1;'),
    (K, 'vectors', 'a cancelled run does not end the streak', '.rposition(|run| !failed(run))', '.rposition(|run| run.status == "completed")'),
    (K, 'vectors', 'any answer of a job that answers requests is a success', '    if template == SCHEDULED_TASK {\n        return true;\n    }', '    if template == SCHEDULED_TASK || run.demand_result.is_some() {\n        return true;\n    }'),
    (K, 'vectors', 'an answer made another way than the job was set is a success', 'frozen.get("executor").and_then(Value::as_str) == Some("ephemeral")', 'frozen.get("executor").is_some()'),
    (K, 'vectors', 'an item the owner archived is made again', '            Some(existing)\n                if existing.archived_at.is_some()\n                    || existing.deleted_at.is_some()', '            Some(existing)\n                if existing.deleted_at.is_some()'),
    (K, 'vectors', 'a recovery leaves the old item open', '.filter(|alert| !(active && alert.reference_id == reference))', '.filter(|_| !active)'),
    (K, 'vectors', 'a janitor turned off keeps its item', '    config.enabled && agent.is_some_and(', '    agent.is_some_and('),
    (K, 'vectors', 'the error keeps its secrets', 'strip_hidden_blocks(&redact(error))', 'strip_hidden_blocks(error)'),
    (K, 'vectors', 'what a model thought stays in the error', '        diagnostic = thought.replace_all(&diagnostic, "").into_owned();', '        let _ = thought;'),
    (K, 'vectors', 'archived items are shown without being asked for', '&& (include_archived || alert.archived_at.is_none())', '&& (include_archived || alert.archived_at.is_none() || true)'),
    # The Host's side.
    (J, 'host', 'a change of the book leaves its items as they were', '    let raised = after.reconcile_attention(&Around::new(app), app.clock.now_ms());', '    let raised = 0;'),
    (J, 'host', 'what the owner is shown is not what the book keeps', '    app.janitors.mirror(after);\n', ''),
    (J, 'host', 'the apps are not told to read the janitors again', '"kind": "janitor-attention"', '"kind": "janitor-changed"'),
    (L, 'host', '/attention lists no janitor items', '"janitor_items": app.janitors.pending(app, include_archived),', '"janitor_items": Vec::<Value>::new(),'),
    (L, 'host', 'the badge leaves the janitors out', '    items.extend(app.janitors.pending(app, false).iter().map(agent_of));\n', ''),
    (L, 'host', 'an item cannot be archived or discarded by its id', 'put_away.filter(|_| clarp_kernel::janitors::attention::is_alert(&artifact_id))', 'put_away.filter(|_| false)'),
    (L, 'host', 'a discarded item still opens', 'let alert = alert.filter(|alert| alert.deleted_at.is_none())?;', 'let alert = alert?;'),
    # Kept, then shown: a write that fails tells nobody anything.
    (J, 'order', 'a change is shown before it is kept', '    written.await?;\n    shown();\n', '    shown();\n    written.await?;\n'),
    (J, 'order', 'a change is shown though its write failed', '    written.await?;\n    shown();\n', '    let _ = written.await;\n    shown();\n'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
