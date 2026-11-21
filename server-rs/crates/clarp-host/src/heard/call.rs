//! A validated call clip uses one fleet settings snapshot for all its attempts.
use super::*;
use clarp_kernel::call_transcription::{self as rules, Policy, Resolved};

pub(super) fn policy(app: &App, directory: &Directory) -> Policy {
    Policy::read(
        directory.setting(rules::LANGUAGES).unwrap_or(""),
        directory.setting(rules::ENGINES).unwrap_or(""),
        &app.ears.default_engine(directory),
        &format!("{OWN_PROVIDER}:{}", app.config.whisper_model),
    )
}
fn resolve(
    app: &App,
    directory: &Directory,
    policy: &Policy,
    session: &str,
    requested: &str,
) -> Resolved {
    let member = directory
        .members
        .iter()
        .find(|m| m.record.session == session);
    let name = member.map_or("", |m| m.record.persona.as_str());
    let who = Who {
        agent_id: member.map_or("", |m| m.record.agent_id.as_str()),
        name,
        persona: name,
        session,
    };
    let langs = languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY));
    let models = languages::stored(directory.setting(languages::AGENT_ENGINES_KEY));
    let own = languages::of_agent(&models, who)
        .map(languages::engines)
        .unwrap_or_default();
    let global = languages::selected(
        directory.setting(languages::LANGUAGE_KEY),
        &app.ears.language,
    );
    let mut resolved = policy.resolve(languages::of_agent(&langs, who), &own, &global, requested);
    if resolved.fields["engines"]["source"] == "global"
        && app.ears.inventory.path(&policy.global).is_some()
    {
        resolved.order = vec![policy.global.clone()];
        resolved.fields["engines"]["value"] = json!([policy.global]);
    }
    resolved
}
pub(crate) fn project(app: &App, calls: &clarp_kernel::calls::Calls, mut answer: Value) -> Value {
    let call = &answer["call"];
    if call["state"] != "live" {
        return answer;
    }
    let directory = app.fleet.directory();
    let policy = policy(app, &directory);
    let listen = call["call_id"]
        .as_str()
        .and_then(|id| calls.record(id))
        .and_then(|(_, record)| record.ring())
        .is_some_and(|r| r["opening_mode"] == "listen_first");
    let participants = call["participants"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|p| {
            let session = p["session"].as_str()?;
            Some((
                session.into(),
                resolve(app, &directory, &policy, session, ""),
            ))
        });
    if let Some(view) = rules::participants_view(&policy, true, listen, participants) {
        answer["call"]["transcription"] = view;
    }
    answer
}
pub(super) async fn hear(
    app: &App,
    session: &str,
    requested: &str,
    hands_free: bool,
    trace: &str,
    clip: (&axum::body::Bytes, &str),
) -> Result<(Heard, Value, i64), Told> {
    if !app.ears.available() {
        return Err(refuse(StatusCode::SERVICE_UNAVAILABLE, heard::DISABLED));
    }
    let directory = app.fleet.directory();
    let policy = policy(app, &directory);
    if !policy.error.is_empty() {
        let engines: Vec<_> = policy
            .engines
            .iter()
            .map(|model| rules::concrete(model, &policy.local))
            .collect();
        return Err((
            StatusCode::CONFLICT,
            json!({"ok":false,"error":"call policy invalid","call_policy_error":policy.error,"language":policy.language,"engines":engines,"attempts":[]}),
        ));
    }
    let mut resolved = resolve(app, &directory, &policy, session, requested);
    if resolved.allows_long_model() {
        let long = app.ears.long_model(&directory, clip.0, clip.1).await;
        resolved.use_long_model(long);
    }
    let language = resolved.fields["language"]["value"].as_str().unwrap_or("");
    let steps = rules::plan(
        &resolved.order,
        policy.configured().then_some(language),
        &policy.local,
    );
    let mut report = rules::Attempts::new(&resolved);
    let mut contacted = engine_order::Contacts::default();
    let (audit, run_id) = if let Some(selected) = steps.iter().find(|step| step.skip.is_none()) {
        crate::vocabulary::compile(app, session, &selected.engine, hands_free, Some(trace))
            .await
            .unwrap_or_default()
    } else {
        (Value::Null, 0)
    };
    let listen_for = audit["payload"].as_str().unwrap_or("");
    for (i, step) in steps.iter().enumerate() {
        if let Some(reason) = step.skip_reason(&contacted) {
            report.skipped(&step.engine, reason);
            continue;
        }
        let began = std::time::Instant::now();
        let mut handed = None;
        let model = if step.local_default && resolved.fields["engines"]["source"] != "device" {
            ""
        } else {
            &resolved.order[i]
        };
        let lang = step
            .language
            .as_ref()
            .and_then(|v| v.as_deref())
            .unwrap_or(language);
        let later = &steps[i + 1..];
        let result = app
            .ears
            .try_hearing(
                model,
                lang,
                listen_for,
                clip,
                &mut handed,
                rules::has_runnable_step(later, &contacted),
            )
            .await;
        app.capture.transcription(
            app.clock.now_ms(),
            result.as_ref().err().map(AttemptError::health_word),
            clip.0.len(),
            Some(began),
        );
        let elapsed = began.elapsed().as_millis() as u64;
        match result {
            Ok(said) => {
                report.heard(
                    &step.engine,
                    handed.as_deref(),
                    elapsed,
                    step.adapted == Some(true),
                );
                return Ok((said, report.finish(), run_id));
            }
            Err(error) => {
                let reason = error.word();
                report.failed(&step.engine, handed.as_deref(), elapsed, reason);
                contacted.failed(&step.engine, handed.is_some());
                if !rules::has_runnable_step(later, &contacted) {
                    let (status, mut body) = error.told();
                    if status == StatusCode::INTERNAL_SERVER_ERROR {
                        body["stt"] = report.finish();
                    }
                    return Err((status, body));
                }
            }
        }
    }
    Err((
        StatusCode::CONFLICT,
        json!({"ok":false,"error":"no compatible engine","language":language,"engines":steps.iter().map(|step|&step.engine).collect::<Vec<_>>(),"attempts":report.finish()["attempts"]}),
    ))
}
