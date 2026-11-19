#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The products' own times a test may make shorter (no test waits one out in real time): the hold of the Oracle's
second voice after it failed (`--second-voice-hold-ms`), how long a silent phone keeps a live call
(`--phone-idle-ms`), and how long the explainer's model is given (`--explain-within-ms`): each read, defaulted to the
product's time, refused in words when it is no number, and used where the time was written before.
"""
import runner

RUNS = {
    'config': ['test', '-p', 'clarp-host', '--lib', 'config::tests'],
    'voice': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_uses_its_second_voice'],
    'phone': ['test', '--test', 'host', '--',
              'a_live_call_is_over_when_its_phone_gives_no_sign_of_life_and_goes_on_while_it_only_asks'],
    'explain': ['test', '--test', 'host', '--',
                'inherited_models_retry_only_the_isolated_question_and_record_the_actual_model'],
}
C = 'clarp-host/src/config.rs'
G = 'clarp-kernel/src/oracle/gemini.rs'
U = 'clarp-host/src/oracle/upstream.rs'
L = 'clarp-host/src/calls/live.rs'
E = 'clarp-host/src/explain.rs'
# The rule of the hold itself (`healthy_for`, and that a held voice is passed over) is planted in oracle-gemini.py.
PLANTS = [
    # ---- the hold of the second voice
    (G, 'config', "the second voice's hold is a minute", 'pub const HOLD_SECONDS: f64 = 600.;', 'pub const HOLD_SECONDS: f64 = 60.;'),
    (C, 'config', 'the hold is not read', '                "--second-voice-hold-ms" => {', '                "--second-voice-hold" => {'),
    (C, 'config', 'the hold that is no number is refused in other words',
     '                        .map_err(|_| "--second-voice-hold-ms needs a number".to_string())?;',
     '                        .map_err(|_| "--second-voice-hold-ms is no number".to_string())?;'),
    (C, 'config', 'the hold is defaulted to nothing', '        let mut second_voice_hold_ms = SECOND_VOICE_HOLD_MS;', '        let mut second_voice_hold_ms = 0;'),
    (U, 'voice', "the Host's hold is the product's whatever it was told",
     '                .healthy(Duration::from_millis(app.config.second_voice_hold_ms))',
     '                .healthy(Duration::from_millis(crate::config::SECOND_VOICE_HOLD_MS))'),
    # ---- how long a silent phone keeps a live call
    (C, 'config', 'the silence is not read', '                "--phone-idle-ms" => {', '                "--phone-idle" => {'),
    (C, 'config', 'a silence that is no number is refused in other words',
     '                        .map_err(|_| "--phone-idle-ms needs a number".to_string())?;',
     '                        .map_err(|_| "--phone-idle-ms is no number".to_string())?;'),
    (C, 'config', 'the silence is defaulted to a minute', '        let mut phone_idle_ms = PHONE_IDLE_MS;', '        let mut phone_idle_ms = 60_000;'),
    (L, 'phone', "the Host's silence is the product's whatever it was told",
     '    let idle = Duration::from_millis(app.config.phone_idle_ms);',
     '    let idle = Duration::from_millis(crate::config::PHONE_IDLE_MS);'),
    # ---- how long the explainer's model is given
    (C, 'config', "the explainer's time is not read", '                "--explain-within-ms" => {', '                "--explain-within" => {'),
    (C, 'config', "an explainer's time that is no number is refused in other words",
     '                        .map_err(|_| "--explain-within-ms needs a number".to_string())?;',
     '                        .map_err(|_| "--explain-within-ms is no number".to_string())?;'),
    (C, 'config', "the explainer's time is defaulted to a minute", 'pub const EXPLAIN_WITHIN_MS: u64 = 45_000;', 'pub const EXPLAIN_WITHIN_MS: u64 = 60_000;'),
    (E, 'explain', "the Host's explainer time is the product's whatever it was told",
     '            let within = Duration::from_millis(app.config.explain_within_ms);',
     '            let within = Duration::from_millis(crate::config::EXPLAIN_WITHIN_MS);'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
