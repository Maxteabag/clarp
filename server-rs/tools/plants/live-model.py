#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The model of GPT-Live this Host runs: the owner's (`[openai] live_model`, read once where the configuration is read),
one of `gpt-live-1` and `gpt-live-1.1`, the latter where none is named, another refused in one line; and what the
Host advertises of the Oracle's calls and what a call opens its voice with are one value from one place.
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--lib', 'live_call::live_models'],
    'host': ['test', '--test', 'host', '--', 'live_model::'],
}
K = 'clarp-kernel/src/live_call.rs'
C = 'clarp-host/src/config.rs'
O = 'clarp-host/src/oracle.rs'
L = 'clarp-host/src/oracle/call.rs'
G = 'clarp-kernel/src/rings/live.rs'
S = 'clarp-kernel/src/live_session.rs'
A = 'clarp-host/src/calls.rs'
PLANTS = [
    (K, 'kernel', 'the older model is not allowed', 'pub const LIVE_MODELS: [&str; 2] = ["gpt-live-1", "gpt-live-1.1"];',
     'pub const LIVE_MODELS: [&str; 2] = ["gpt-live-1.1", "gpt-live-1.1"];'),
    (K, 'kernel', 'the default is the older model', 'pub const LIVE_MODEL_DEFAULT: &str = "gpt-live-1.1";',
     'pub const LIVE_MODEL_DEFAULT: &str = "gpt-live-1";'),
    (K, 'kernel', 'any name the owner writes runs', '            .copied()\n            .ok_or(LIVE_MODEL_DEFAULT),',
     '            .copied()\n            .or(Some(LIVE_MODEL_DEFAULT))\n            .ok_or(LIVE_MODEL_DEFAULT),'),
    (C, 'host', 'a refused name is not said', '        eprintln!(\n            "config.openai.live_model refused:', '        let _ = (\n            "config.openai.live_model refused:'),
    (O, 'host', "the Oracle advertises the reference's word, not the owner's model",
     '    let (model, voice) = (app.config.live_model, oracle::voice::voice());', '    let (model, voice) = (oracle::voice::model(), oracle::voice::voice());'),
    (L, 'host', "the Oracle's call opens with another model than it advertises", '        model: app.config.live_model,',
     '        model: voice::model(),'),
    # ---- an agent's live call (C): the model chosen for the client, kept with the call, named alike
    (K, 'kernel', 'a client name in other letters is the same name', '            .any(|(name, minimum)| name == platform && *minimum >= 1 && contract >= *minimum)',
     '            .any(|(name, minimum)| name.eq_ignore_ascii_case(platform) && *minimum >= 1 && contract >= *minimum)'),
    (K, 'kernel', 'a contract at the minimum is below it', '            .any(|(name, minimum)| name == platform && *minimum >= 1 && contract >= *minimum)',
     '            .any(|(name, minimum)| name == platform && *minimum >= 1 && contract > *minimum)'),
    (K, 'kernel', 'a minimum of nought admits every contract', '            .any(|(name, minimum)| name == platform && *minimum >= 1 && contract >= *minimum)',
     '            .any(|(name, minimum)| name == platform && contract >= *minimum)'),
    (K, 'kernel', "a client's minimum of the apps is not ios's", 'pub const AGENT_LIVE_MODEL_MIN_CLIENT: [(&str, i64); 2] = [("ios", 75), ("android", 29)];',
     'pub const AGENT_LIVE_MODEL_MIN_CLIENT: [(&str, i64); 2] = [("ios", 76), ("android", 29)];'),
    (C, 'host', 'a key of the table is taken in lower case', '                None => minimums.push((name.clone(), minimum)),',
     '                None => minimums.push((name.to_lowercase(), minimum)),'),
    (C, 'host', "the owner's minimum for a name is not taken", '                Some(known) => known.1 = minimum,', '                Some(_) => {}'),
    (G, 'host', 'a grant names another model than the call keeps', '"model": live.running(), "voice": live_call::VOICE,',
     '"model": live_call::MODEL, "voice": live_call::VOICE,'),
    (G, 'host', "the answering client's model is not kept with the call", '            live.model = model.to_string();', '            let _ = model;'),
    (G, 'host', 'the join names another model than the grant', '            model: live.running().to_string(),', '            model: live_call::MODEL.to_string(),'),
    (S, 'host', "the session's frame names another model than its call", '"model": call.model, "voice": live_call::VOICE,',
     '"model": live_call::MODEL, "voice": live_call::VOICE,'),
    (S, 'host', 'the voice is begun with another model than its call', '                "model": self.call.model,', '                "model": live_call::MODEL,'),
    (A, 'host', "a ring's model is chosen without its answering client", '        client: crate::render_spec::client(&headers),', '        client: None,'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
