#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What this Host changed on 18 Nov 2026 to follow the Python Host's fixes of that evening: a number of a cron
expression is ASCII digits only (its PR 427, R66), and a janitor put back as it came gets its job's own settings
and way of running (its PR 425, R30).
"""
import runner

RUNS = {
    'schedules': ['test', '--test', 'schedule_vectors'],
    'stories': ['test', '--test', 'janitor_stories'],
}
S = 'clarp-kernel/src/schedules.rs'
J = 'clarp-kernel/src/janitors.rs'
B = 'clarp-kernel/src/janitors/book.rs'
# NOT PLANTED, each with its reason:
#   schedules.rs: that a number of no signs is none (`!text.is_empty() &&`): `python::int` refuses an empty text in
#     the same words.
#   schedules.rs: that the digits of another script are no number (the bytes are ASCII digits): `python::int` reads
#     ASCII digits only itself, so the check of the bytes is a second guard of it (taken off after its first run,
#     MISSED).
#   book.rs: that a trigger without a seed is given its defaults (`defaults.map_or(..)`; as nothing, MISSED in the
#     first run): a janitor's configuration fills a trigger's defaults under whatever it is given, so nothing comes
#     out as the defaults; that it is not left as it was set is planted.
#   janitors.rs: that a tool is known by any name it goes by (`tool_named(backend).unwrap_or(backend)`): a janitor's
#     tool is kept by its own name; the other names are taken where it is chosen.
#   book.rs: that the last seed for a trigger counts (`.rev()`): no job seeds one trigger twice.
#   book.rs: that a seed of a way of running is shut off only where it says so truthfully (`python::truthy`): the one
#     seed there is says `true`.
PLANTS = [
    # ---- a number of a cron expression
    (S, 'schedules', 'a number may have a sign', 'text.bytes().all(|sign| sign.is_ascii_digit());', "text.bytes().all(|sign| sign.is_ascii_digit() || sign == b'+');"),
    (S, 'schedules', 'a number may be written with underscores', 'text.bytes().all(|sign| sign.is_ascii_digit());', "text.bytes().all(|sign| sign.is_ascii_digit() || sign == b'_');"),
    (S, 'schedules', 'a number may be negative, as a step', 'text.bytes().all(|sign| sign.is_ascii_digit());', "text.bytes().all(|sign| sign.is_ascii_digit() || sign == b'-');"),
    (S, 'schedules', 'a plain number is no number', 'text.bytes().all(|sign| sign.is_ascii_digit());', "text.bytes().all(|sign| sign.is_ascii_digit() && sign != b'5');"),
    (S, 'schedules', 'what is no number is refused in the older words', '.ok_or_else(|| format!("Not a cron number: {}", python::text(text)))', '.ok_or_else(|| format!("invalid literal for int() with base 10: {}", python::text(text)))'),
    (S, 'schedules', 'what is no number is named without its quotes', '.ok_or_else(|| format!("Not a cron number: {}", python::text(text)))', '.ok_or_else(|| format!("Not a cron number: {text}"))'),
    # ---- a janitor put back as it came
    (J, 'stories', 'no tool can shut a run off', '    catalogue().isolating.iter().any(|id| id == tool)', '    catalogue().isolating.iter().any(|id| id == tool) && false'),
    (J, 'stories', 'every tool can shut a run off', '    catalogue().isolating.iter().any(|id| id == tool)', '    catalogue().isolating.iter().any(|id| id == tool) || true'),
    (B, 'stories', 'the seed of another trigger is taken', '                .find(|seed| seed["trigger_id"] == kept.trigger_id);', '                .find(|seed| seed["trigger_id"] != kept.trigger_id);'),
    (B, 'stories', "a job's seeds are not taken", '            let config = match seed {', '            let config = match seed.filter(|_| false) {'),
    (B, 'stories', 'a trigger without a seed keeps what it was set to', '                None => defaults.map_or(json!({}), |trigger| trigger["defaults"].clone()),', '                None => json!(kept.config),'),
    (B, 'stories', 'a trigger is turned on by a reset', '"trigger_version": kept.trigger_version, "enabled": kept.enabled,\n                "config": config,', '"trigger_version": kept.trigger_version, "enabled": true,\n                "config": config,'),
    (B, 'stories', "a job's way of running is not taken", '            Map::new()\n        } else {\n            seeded\n        };', '            Map::new()\n        } else {\n            Map::new()\n        };'),
    (B, 'stories', 'a run is never shut off after a reset', '        let execution = if shut_off && !super::isolates(&agent.backend) {', '        let execution = if shut_off {'),
    (B, 'stories', 'a run is shut off after a reset whatever its tool', '        let execution = if shut_off && !super::isolates(&agent.backend) {', '        let execution = if false {'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
