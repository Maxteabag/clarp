//! `--check artifacts --out DIR`: every artifact type is embedded in the
//! chat under the reply it was made in, shows what the iOS card shows and
//! does what it offers. Each type's stages load a chat of that type's
//! fixtures from the fake Host (`/__control/artifact-chat`) and check the
//! card's model fields and the interaction; each takes a screenshot.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, headless, posts, report, rows, run_stages, shot};
use crate::{ArtifactBridge, ArtifactItem};

/// Every card in the open chat, in transcript order, with its row's id.
fn cards(window: &crate::AppWindow) -> Vec<(String, ArtifactItem)> {
    rows(window).iter().flat_map(|row| row.artifacts.iter().map(|a| (row.id.to_string(), a)).collect::<Vec<_>>()).collect()
}

fn card(window: &crate::AppWindow, id: &str) -> Option<ArtifactItem> {
    cards(window).into_iter().map(|(_, a)| a).find(|a| a.id == id)
}

/// The fixture artifact as the Host listed it.
fn fixture(app: &crate::App, id: &str) -> Value {
    app.engine.borrow().update_artifacts().iter().find(|a| a["artifact_id"] == id).cloned().unwrap_or(Value::Null)
}

/// A stage that gives a new agent, `session`, a chat of `types`' fixtures,
/// and a stage that opens it once the roster lists it.
fn load_chat(session: &'static str, types: &'static [&'static str]) -> Vec<Stage> {
    vec![
        ("load", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let loaded = control("/__control/artifact-chat", &json!({"session": session, "types": types}));
            check(loaded.is_ok(), &format!("the Host takes a chat of {types:?} artifacts {}", loaded.err().unwrap_or_default()));
            true
        })),
        ("open", Box::new(move |app, _, _| {
            if app.engine.borrow().roster().find(session).is_none() {
                return false;
            }
            app.engine.borrow_mut().select(session);
            crate::pump();
            true
        })),
    ]
}

/// Waits until every id shows as a card and the chat has settled.
fn placed(window: &crate::AppWindow, ids: &[&str], elapsed: Duration) -> bool {
    elapsed >= Duration::from_millis(400) && ids.iter().all(|id| card(window, id).is_some())
}

// ---- countdown

fn countdown_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let out2 = out.clone();
    let first: Rc<RefCell<(i32, String)>> = Rc::default();
    let first2 = first.clone();
    let ticks = Rc::new(Cell::new(0));
    let mut stages = load_chat("art-countdown", &["countdown"]);
    stages.extend::<Vec<Stage>>(vec![
        ("countdown cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &["cd-launch", "cd-reached", "cd-past", "cd-cancelled", "cd-broken"], elapsed) {
                return false;
            }
            let bridge = window.global::<ArtifactBridge>();
            let now = bridge.get_now();
            let launch = card(window, "cd-launch").expect("launch");
            let placed_under = cards(window).iter().find(|(_, a)| a.id == "cd-launch").map(|(row, _)| row.clone()).unwrap_or_default();
            check(placed_under == "made-cd-launch", &format!("a countdown sits under the reply it was made in: {placed_under}"));
            check(launch.kind == "countdown" && launch.label == "COUNTDOWN" && launch.badge.is_empty(), &format!("it is labelled a countdown, no badge while active: {:?} {:?}", launch.label, launch.badge));
            check(launch.summary == "Freeze starts an hour before.", "it shows its summary");
            check(launch.note == "Remember to **page** the on-call.", &format!("and its Markdown content under it: {:?}", launch.note));
            // The target's own date and time (it carries its offset), and its zone.
            let target = fixture(app, "cd-launch")["target_at"].as_str().unwrap_or_default().to_owned();
            let expected = chrono::DateTime::parse_from_rfc3339(&target).map(|t| format!("{} · Europe/Oslo", t.format("%b %-d, %Y at %H:%M"))).unwrap_or_default();
            check(launch.countdown_set && launch.countdown_line == expected.as_str(), &format!("the target line: {:?} (want {expected:?})", launch.countdown_line));
            let phase = bridge.invoke_countdown_phase(launch.countdown_at, now);
            let clock = bridge.invoke_countdown_clock(launch.countdown_at, now);
            check(phase == "Remaining" && clock.starts_with("1d 02:03:0"), &format!("a day and two hours ahead: {phase} {clock}"));
            let reached = card(window, "cd-reached").expect("reached");
            let (phase, clock) = (bridge.invoke_countdown_phase(reached.countdown_at, now), bridge.invoke_countdown_clock(reached.countdown_at, now));
            check(reached.countdown_set && phase == "Target reached" && clock == "Now", &format!("within a minute after the target: {phase} {clock}"));
            let past = card(window, "cd-past").expect("past");
            let (phase, clock) = (bridge.invoke_countdown_phase(past.countdown_at, now), bridge.invoke_countdown_clock(past.countdown_at, now));
            check(past.countdown_set && phase == "Since target" && clock.starts_with("02:05:"), &format!("two hours after: {phase} {clock}"));
            let cancelled = card(window, "cd-cancelled").expect("cancelled");
            check(cancelled.badge == "Cancelled" && cancelled.countdown_note == "Cancelled" && !cancelled.countdown_set, &format!("a cancelled one says so and has no clock: {:?} {:?}", cancelled.badge, cancelled.countdown_note));
            let broken = card(window, "cd-broken").expect("broken");
            check(!broken.countdown_set && broken.countdown_note == "Countdown unavailable", &format!("an unreadable target: {:?}", broken.countdown_note));
            *first.borrow_mut() = (now, bridge.invoke_countdown_clock(launch.countdown_at, now).to_string());
            true
        })),
        ("countdown ticks", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ArtifactBridge>();
            let (then, clock) = first2.borrow().clone();
            let now = bridge.get_now();
            if now <= then {
                ticks.set(ticks.get() + 1);
                // Two seconds and no tick: the check fails below.
                if elapsed < Duration::from_millis(2000) {
                    return false;
                }
            }
            let launch = card(window, "cd-launch").expect("launch");
            let later = bridge.invoke_countdown_clock(launch.countdown_at, now);
            check(now > then && later.as_str() != clock, &format!("the clock ticks: {clock} then {later} (now {then} then {now})"));
            shot(&out, "artifacts-01-countdown");
            app_now().focus_transcript();
            true
        })),
        ("countdown from the top", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("countdown at the top", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            // Again, once the list has measured the rows it estimated.
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("long countdown in view", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            // The long title wrapped, the summary and the content.
            shot(&out2, "artifacts-01b-countdown-top");
            true
        })),
    ]);
    stages
}

/// Lines the app recorded instead of opening them (`CLARP_TEST_OPEN_URL`).
fn opened() -> Vec<String> {
    std::env::var_os("CLARP_TEST_OPEN_URL").and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default().lines().map(str::to_owned).collect()
}

/// A plain HTTP/1.1 request to a loopback `url`, as a browser would send
/// it; `host` replaces the Host header. The status and the body.
fn fetch(method: &str, url: &str, body: &str, host: Option<&str>) -> Result<(u16, String), String> {
    use std::io::{Read, Write};
    let rest = url.strip_prefix("http://").ok_or_else(|| format!("not http: {url}"))?;
    let (authority, path) = rest.split_once('/').map(|(a, p)| (a, format!("/{p}"))).unwrap_or((rest, "/".into()));
    if !authority.starts_with("127.0.0.1:") {
        return Err(format!("not loopback: {url}"));
    }
    let mut stream = std::net::TcpStream::connect(authority).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).map_err(|e| e.to_string())?;
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {}\r\nOrigin: http://{authority}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        host.unwrap_or(authority),
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply).map_err(|e| e.to_string())?;
    let status = reply.split(' ').nth(1).and_then(|s| s.parse().ok()).ok_or_else(|| format!("no status: {reply:?}"))?;
    Ok((status, reply.split_once("\r\n\r\n").map(|(_, b)| b.to_owned()).unwrap_or_default()))
}

/// The one selected card's id ("" for none, "many" for more than one).
fn selected(window: &crate::AppWindow) -> String {
    let chosen: Vec<String> = cards(window).into_iter().filter(|(_, a)| a.selected).map(|(_, a)| a.id.to_string()).collect();
    match chosen.len() {
        0 => String::new(),
        1 => chosen[0].clone(),
        _ => "many".into(),
    }
}

// ---- html_form

fn html_form_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let form_url: Rc<RefCell<String>> = Rc::default();
    let (form_url2, form_url3) = (form_url.clone(), form_url.clone());
    let mut stages = load_chat("art-form", &["html_form"]);
    stages.extend::<Vec<Stage>>(vec![
        ("form cards", Box::new(|app, window, elapsed| {
            if !placed(window, &["form-trip", "form-report", "form-stale"], elapsed) {
                return false;
            }
            let trip = card(window, "form-trip").expect("trip");
            check(trip.label == "FORM" && trip.action == "Open form" && trip.summary == "Pick where we go and for how long.", &format!("an interactive form offers to open: {:?} {:?}", trip.label, trip.action));
            let report_card = card(window, "form-report").expect("report");
            check(report_card.label == "REPORT" && report_card.action == "Open report", &format!("a read-only one is a report: {:?} {:?}", report_card.label, report_card.action));
            check(selected(window).is_empty(), "no card is selected before the keyboard asks");
            app.focus_transcript();
            true
        })),
        ("keyboard on the chat", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            // K from the chat selects the latest card, then the ones above.
            headless::press("k");
            true
        })),
        ("latest card selected", Box::new(|_, window, elapsed| {
            if selected(window) != "form-stale" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K selects the latest card: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            check(true, "K selects the latest card");
            headless::press("k");
            headless::press("k");
            true
        })),
        ("first form selected", Box::new(|_, window, elapsed| {
            if selected(window) != "form-trip" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K again moves up the cards: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            check(true, "K again moves up the cards, one at a time");
            headless::press("j");
            headless::press("k");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("form opens in the browser", Box::new(move |_, _, elapsed| {
            let Some(url) = opened().into_iter().find(|u| u.contains("/form/")) else {
                if elapsed > Duration::from_secs(3) {
                    check(false, &format!("Enter opens the form in the browser: {:?}", opened()));
                    return true;
                }
                return false;
            };
            check(url.starts_with("http://127.0.0.1:"), &format!("Enter opens the form from a loopback page: {url}"));
            let page = fetch("GET", &url, "", None);
            let page_ok = page.as_ref().is_ok_and(|(status, body)| {
                *status == 200 && body.contains("name=\"destination\"") && body.contains("window.clarpForm") && body.contains("Content-Security-Policy")
            });
            check(page_ok, &format!("the page is the form with the answer bridge and a strict policy: {:?}", page.as_ref().map(|(s, b)| (*s, b.len()))));
            let foreign = fetch("GET", &url, "", Some("evil.example:80"));
            check(foreign.as_ref().is_ok_and(|(status, _)| *status == 403), &format!("another Host name is refused: {:?}", foreign.map(|(s, _)| s)));
            let guessed = fetch("GET", &format!("{}/form/not-the-token", url.split("/form/").next().unwrap_or_default()), "", None);
            check(guessed.as_ref().is_ok_and(|(status, _)| *status == 404), &format!("a guessed page is not found: {:?}", guessed.map(|(s, _)| s)));
            let draft = fetch("POST", &format!("{url}/draft"), r#"{"destination":"Troms"}"#, None);
            check(draft.as_ref().is_ok_and(|(status, _)| *status == 200), &format!("the page saves a draft: {:?}", draft.map(|(s, _)| s)));
            let sent = fetch("POST", &format!("{url}/submit"), r#"{"destination":"Tromsø","nights":3,"train":true}"#, None);
            check(sent.as_ref().is_ok_and(|(status, _)| *status == 202), &format!("the page hands its answers over: {:?}", sent));
            *form_url.borrow_mut() = url;
            true
        })),
        ("answers reach the Host", Box::new(|_, window, elapsed| {
            let submitted = posts("/artifacts/form-trip/submit");
            let accepted = card(window, "form-trip").is_some_and(|c| c.status_text == "Answers accepted");
            if (submitted.is_empty() || !accepted) && elapsed < Duration::from_secs(3) {
                return false;
            }
            let body = submitted.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body["answers"] == json!({"destination": "Tromsø", "nights": 3, "train": true}) && body["version"] == 3, &format!("the answers reach the Host with the form's version: {body}"));
            check(body["submission_id"].as_str().is_some_and(|id| id.len() >= 32), "with a submission id");
            check(accepted, &format!("the card says the answers were accepted: {:?}", card(window, "form-trip").map(|c| c.status_text)));
            window.global::<ArtifactBridge>().invoke_open("form-report".into());
            true
        })),
        ("report opens in the viewer", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("a report opens in the report viewer: overlay {:?}", window.get_overlay()));
                    return true;
                }
                return false;
            }
            check(window.get_report_title() == "Quarterly cost report" && window.get_report_blocks().row_count() > 0, "a report opens in the report viewer");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("stale form", Box::new(move |_, window, elapsed| {
            if !window.get_overlay().is_empty() || elapsed < Duration::from_millis(200) {
                return false;
            }
            let before = opened().len();
            window.global::<ArtifactBridge>().invoke_open("form-stale".into());
            check(opened().len() == before + 1 && opened().last().is_some_and(|u| u.contains("/form/") && *u != *form_url2.borrow()), "another form gets its own page");
            if let Some(url) = opened().last() {
                let sent = fetch("POST", &format!("{url}/submit"), r#"{"dish":"soup"}"#, None);
                check(sent.as_ref().is_ok_and(|(status, _)| *status == 202), "its answers are handed over");
            }
            true
        })),
        ("stale answers refused", Box::new(move |_, window, elapsed| {
            let status = card(window, "form-stale").map(|c| c.status_text.to_string()).unwrap_or_default();
            if !status.starts_with("Not sent") && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(status.starts_with("Not sent") && status.contains("form version changed"), &format!("a refused answer says why: {status:?}"));
            let _ = &form_url3;
            shot(&out, "artifacts-02-html-form");
            app_now().focus_transcript();
            true
        })),
        ("a click opens a card", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            // A real pointer click, down the left of the chat from the
            // bottom: the latest row is a card, the rows above are prose.
            let before = opened().len();
            let mut clicked = None;
            for y in (300..=700).rev().step_by(12) {
                super::click_at(window, 560.0, y as f32);
                if opened().len() > before {
                    clicked = Some(y);
                    break;
                }
            }
            let url = opened().last().cloned().unwrap_or_default();
            check(clicked.is_some() && url.contains("/form/"), &format!("clicking the latest card opens its form (at y {clicked:?}): {url}"));
            check(selected(window) == "form-stale", &format!("and puts the keyboard on it: {:?}", selected(window)));
            true
        })),
    ]);
    stages
}

/// Stages that take the chat to its top (twice: the list corrects its
/// estimates after the first jump) and save a shot there.
fn top_shot(out: &str, name: &'static str) -> Vec<Stage> {
    let out = out.to_owned();
    vec![
        ("to the top", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("at the top", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("top shot", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            shot(&out, name);
            true
        })),
    ]
}

// ---- decision

fn decision_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let out2 = out.clone();
    let ids = ["dec-deploy", "dec-refused", "dec-discard", "dec-approved", "dec-declined", "dec-expired", "dec-waiting", "dec-missing", "dec-click"];
    let mut stages = load_chat("art-decision", &["decision"]);
    stages.extend::<Vec<Stage>>(vec![
        ("decision cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let deploy = card(window, "dec-deploy").expect("deploy");
            check(deploy.label == "DECISION" && deploy.pending && deploy.approval, &format!("a pending approval: {:?} pending {} approval {}", deploy.label, deploy.pending, deploy.approval));
            check(deploy.question == "Deploy the billing migration tonight?" && deploy.context.starts_with("All checks are green"), &format!("it asks its question with its context: {:?}", deploy.question));
            check(deploy.meta.contains("Blocks the release train") && deploy.meta.contains("Needs a closer look") && deploy.meta.contains("Time sensitive"), &format!("why, how much effort, how urgent: {:?}", deploy.meta));
            let labels: Vec<String> = deploy.options.iter().map(|o| o.label.to_string()).collect();
            check(labels == ["Ship it", "Hold"] && deploy.chosen == -1, &format!("its own Yes and No labels, none chosen: {labels:?} {}", deploy.chosen));
            let discard = card(window, "dec-discard").expect("discard");
            let plain: Vec<String> = discard.options.iter().map(|o| o.label.to_string()).collect();
            check(plain == ["Yes", "No"] && discard.meta.starts_with("Due "), &format!("the default labels and a due date: {plain:?} {:?}", discard.meta));
            for (id, resolved, ok) in [("dec-approved", "Approved", true), ("dec-declined", "Declined", false), ("dec-expired", "Expired", false)] {
                let shown = card(window, id).expect(id);
                check(!shown.pending && shown.resolved == resolved && shown.resolved_ok == ok && shown.options.row_count() == 0, &format!("{id} shows {resolved:?}: {:?}", shown.resolved));
            }
            let waiting = card(window, "dec-waiting").expect("waiting");
            check(waiting.delivery == "Saved. Waiting to deliver to the agent.", &format!("a saved answer not yet delivered says so: {:?}", waiting.delivery));
            let missing = card(window, "dec-missing").expect("missing");
            check(!missing.pending && missing.question.is_empty() && missing.resolved.is_empty(), "a decision without its question shows only its title");
            app.focus_transcript();
            true
        })),
        ("decision keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            for _ in 0..9 {
                headless::press("k");
            }
            true
        })),
        ("decision chosen", Box::new(|_, window, elapsed| {
            if selected(window) != "dec-deploy" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K reaches the first decision: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            headless::press("2");
            let hold = card(window, "dec-deploy").map(|c| c.chosen).unwrap_or(-2);
            headless::press("1");
            let ship = card(window, "dec-deploy").map(|c| c.chosen).unwrap_or(-2);
            check(hold == 1 && ship == 0, &format!("2 chooses Hold, 1 Ship it: {hold} {ship}"));
            check(posts("/decisions/d-deploy/resolve").is_empty(), "choosing sends nothing yet");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("decision sent", Box::new(|_, window, elapsed| {
            let sent = posts("/decisions/d-deploy/resolve");
            let shown = card(window, "dec-deploy").is_some_and(|c| !c.pending && c.resolved == "Approved");
            if (sent.is_empty() || !shown) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let body = sent.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body == json!({"choice": "accepted", "expected_revision": 4}), &format!("Enter approves against the revision seen: {body}"));
            check(shown, &format!("the card shows it approved: {:?}", card(window, "dec-deploy").map(|c| c.resolved)));
            // The other ways in: a refused answer, and discarding.
            let bridge = window.global::<ArtifactBridge>();
            bridge.invoke_choose("dec-refused".into(), 1);
            bridge.invoke_send("dec-refused".into());
            bridge.invoke_discard("dec-discard".into());
            true
        })),
        ("decision refused and discarded", Box::new(move |_, window, elapsed| {
            let refused = card(window, "dec-refused").map(|c| c.status_text.to_string()).unwrap_or_default();
            let discarded = card(window, "dec-discard").is_some_and(|c| c.resolved == "Discarded");
            if (!refused.starts_with("Not sent") || !discarded) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let declined = posts("/decisions/d-refused/resolve").last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(declined == json!({"choice": "rejected", "expected_revision": 2}), &format!("the second answer declines: {declined}"));
            check(refused.contains("the decision changed since you saw it") && card(window, "dec-refused").is_some_and(|c| c.pending), &format!("a refused answer says why and stays open: {refused:?}"));
            let dismissed = posts("/decisions/d-discard/dismiss").last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(dismissed == json!({"expected_revision": 4}) && discarded, &format!("discarding dismisses it: {dismissed}"));
            shot(&out, "artifacts-03-decision");
            true
        })),
        ("an approval button is clicked", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            // A real pointer click on the latest card's first button ("Turn
            // on"), found from the bottom of the chat up its left edge.
            let hit = (300..=760).rev().step_by(4).find(|y| {
                super::click_at(window, 440.0, *y as f32);
                !posts("/decisions/d-click/resolve").is_empty()
            });
            let body = posts("/decisions/d-click/resolve").last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(hit.is_some() && body == json!({"choice": "accepted", "expected_revision": 4}), &format!("clicking an approval's button sends it at once (at y {hit:?}): {body}"));
            app_now().focus_transcript();
            true
        })),
    ]);
    stages.extend(top_shot(&out2, "artifacts-03b-decision-top"));
    stages
}

// ---- question

fn question_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["q-trip", "q-custom", "q-picked", "q-written", "q-ranking", "q-click"];
    let mut stages = load_chat("art-question", &["question"]);
    stages.extend::<Vec<Stage>>(vec![
        ("question cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let trip = card(window, "q-trip").expect("trip");
            check(trip.label == "QUESTION" && trip.pending && !trip.approval && trip.allow_custom, &format!("a pending question with a custom answer: {:?} {} {} {}", trip.label, trip.pending, trip.approval, trip.allow_custom));
            let options: Vec<(String, String, bool)> = trip.options.iter().map(|o| (o.label.to_string(), o.detail.to_string(), o.recommended)).collect();
            check(
                options.len() == 3 && options[0] == ("Bergen".into(), "Fjords and rain; seven hours by train from Oslo.".into(), false) && options[1].2 && !options[2].2,
                &format!("its options with their descriptions, the recommended one tagged unless its label says so: {options:?}"),
            );
            check(trip.meta.contains("About a minute") && trip.context.starts_with("Budget is 40k"), &format!("its effort and context: {:?}", trip.meta));
            let picked = card(window, "q-picked").expect("picked");
            check(!picked.pending && picked.resolved == "Answer saved" && picked.answer == "SQLite" && picked.chosen == 1, &format!("an answered one names the option, marked: {:?} {:?} {}", picked.resolved, picked.answer, picked.chosen));
            let written = card(window, "q-written").expect("written");
            check(written.answer == "Let's call it Fjord instead" && written.draft == "Let's call it Fjord instead", &format!("or the answer written: {:?}", written.answer));
            let ranking = card(window, "q-ranking").expect("ranking");
            check(ranking.options.row_count() == 0 && ranking.meta.contains("Update Clarp"), &format!("a kind of question this app cannot answer says so: {:?}", ranking.meta));
            app.focus_transcript();
            true
        })),
        ("question keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            for _ in 0..6 {
                headless::press("k");
            }
            true
        })),
        ("question chosen", Box::new(|_, window, elapsed| {
            if selected(window) != "q-trip" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K reaches the first question: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            headless::press("2");
            let chosen = card(window, "q-trip").map(|c| c.chosen).unwrap_or(-2);
            check(chosen == 1 && posts("/decisions/q-trip/resolve").is_empty(), &format!("2 chooses Tromsø and sends nothing yet: {chosen}"));
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("question answered", Box::new(|_, window, elapsed| {
            let sent = posts("/decisions/q-trip/resolve");
            let shown = card(window, "q-trip").is_some_and(|c| c.resolved == "Answer saved" && c.answer == "Tromsø");
            if (sent.is_empty() || !shown) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let body = sent.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body == json!({"answer": {"option_id": "tromso"}, "expected_revision": 7}), &format!("Enter sends the chosen option: {body}"));
            check(shown, "the card shows the answer saved");
            headless::press("j");
            true
        })),
        ("custom answer", Box::new(|_, window, elapsed| {
            if selected(window) != "q-custom" {
                return elapsed > Duration::from_secs(2) && { check(false, "J moves to the next question"); true };
            }
            // Its last number writes an answer of one's own.
            headless::press("3");
            true
        })),
        ("writing", Box::new(|app, window, elapsed| {
            let editing = card(window, "q-custom").is_some_and(|c| c.chosen == 2 && c.editing);
            if !editing || elapsed < Duration::from_millis(300) {
                return elapsed > Duration::from_secs(2) && { check(false, "3 opens the answer of one's own"); true };
            }
            check(crate::commands::context(app, window) == "composer", "the keyboard types into it (letters are not shortcuts)");
            headless::type_text("Friday, after lunch");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("custom answer sent", Box::new(move |_, window, elapsed| {
            let sent = posts("/decisions/q-custom/resolve");
            let shown = card(window, "q-custom").is_some_and(|c| c.resolved == "Answer saved" && c.answer == "Friday, after lunch");
            if (sent.is_empty() || !shown) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let body = sent.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body == json!({"answer": {"text": "Friday, after lunch"}, "expected_revision": 7}), &format!("Enter in it sends the written answer: {body}"));
            check(shown, "and the card shows it");
            shot(&out, "artifacts-04-question");
            true
        })),
        ("question clicked", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            // Real pointer clicks on the latest card: an option, then Send
            // answer (below the options; a click on it first says to choose).
            let option = (300..=760).rev().step_by(4).find(|y| {
                super::click_at(window, 440.0, *y as f32);
                card(window, "q-click").is_some_and(|c| c.chosen >= 0)
            });
            let chosen = card(window, "q-click").map(|c| c.chosen).unwrap_or(-1);
            let send = (300..=760).rev().step_by(4).find(|y| {
                super::click_at(window, 440.0, *y as f32);
                !posts("/decisions/q-click/resolve").is_empty()
            });
            let body = posts("/decisions/q-click/resolve").last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(option.is_some() && send.is_some() && body["answer"]["option_id"].is_string(), &format!("clicking an option and Send answer sends it (option {chosen} at {option:?}, send at {send:?}): {body}"));
            true
        })),
        ("clicked question answered", Box::new(move |_, window, elapsed| {
            let done = card(window, "q-click").is_some_and(|c| c.resolved == "Answer saved");
            if !done || elapsed < Duration::from_millis(500) {
                return elapsed > Duration::from_secs(4) && { check(false, "the clicked question is answered"); true };
            }
            shot(&out2, "artifacts-04b-question-clicked");
            true
        })),
    ]);
    stages
}

// ---- a chat full of artifacts

/// Every type so far, the one ending on a clickable card last.
const ALL_TYPES: &[&str] = &["countdown", "decision", "question", "html_form"];

/// How far the chat's content moved down between two saved frames (rows
/// of the chat's left half, the best match of their mean brightness).
fn vertical_shift(before: &str, after: &str) -> Result<i32, String> {
    let rows = |path: &str| -> Result<Vec<f32>, String> {
        let image = image::open(path).map_err(|e| format!("{path}: {e}"))?.to_luma8();
        Ok((0..image.height()).map(|y| (400..1040.min(image.width())).map(|x| f32::from(image.get_pixel(x, y)[0])).sum::<f32>() / 640.0).collect())
    };
    let (a, b) = (rows(before)?, rows(after)?);
    let (top, bottom) = (70i32, 700i32.min(a.len() as i32).min(b.len() as i32));
    let mut best = (f32::MAX, 0);
    // Within a card's height: the chat repeats itself every few cards.
    for shift in -150i32..=150 {
        let pairs: Vec<(f32, f32)> = (top..bottom).filter(|y| (top..bottom).contains(&(y + shift))).map(|y| (a[y as usize], b[(y + shift) as usize])).collect();
        if pairs.len() < 250 {
            continue;
        }
        let difference = pairs.iter().map(|(x, y)| (x - y).abs()).sum::<f32>() / pairs.len() as f32;
        if difference < best.0 {
            best = (difference, shift);
        }
    }
    Ok(best.1)
}

/// The chat full of artifacts opens at its latest message and stays there
/// while the cards settle; a reader scrolled up stays put while the cards
/// around them change state and size (summaries grow, decisions are
/// answered, a form's status comes and goes) and new ones arrive.
fn scroll_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let (out2, out3) = (out.clone(), out.clone());
    let offset = Rc::new(Cell::new(0.0f32));
    let offset2 = offset.clone();
    let held = Rc::new(Cell::new(0.0f32));
    let held2 = held.clone();
    let mut stages = vec![
        ("load", Box::new(move |app: &crate::App, _: &crate::AppWindow, _: Duration| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let loaded = control("/__control/artifact-chat", &json!({"session": "art-all", "types": ALL_TYPES, "suffix": "-all"}));
            check(loaded.is_ok(), &format!("the Host takes a chat of every type {}", loaded.err().unwrap_or_default()));
            true
        }) as Box<dyn FnMut(&crate::App, &crate::AppWindow, Duration) -> bool>),
        ("open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("art-all").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("art-all");
            crate::pump();
            true
        })),
    ];
    stages.extend::<Vec<Stage>>(vec![
        ("full chat opens", Box::new(move |_, window, elapsed| {
            let last = cards(window).last().map(|(_, a)| a.id.to_string()).unwrap_or_default();
            if last != "form-stale-all" || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(report().follows && report().at_end, &format!("a chat full of artifacts opens at its latest message (follows {}, at end {}, offset {})", report().follows, report().at_end, report().offset));
            offset.set(report().offset);
            true
        })),
        ("full chat settles", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(1000) {
                return false;
            }
            let moved = (report().offset - offset2.get()).abs();
            check(report().at_end && moved < 1.0, &format!("and stays there while the cards settle: no card changes height ({moved}px)"));
            shot(&out, "artifacts-90-full-chat-latest");
            // The latest card is the last thing in the chat: a click just
            // above the chat's bottom edge lands on it.
            let before = opened().len();
            let bottom = (300..=760).rev().step_by(4).find(|y| {
                super::click_at(window, 560.0, *y as f32);
                opened().len() > before
            });
            let hit = bottom.is_some_and(|y| y >= 640);
            check(hit && opened().last().is_some_and(|u| u.contains("/form/")), &format!("the latest card is at the bottom of the chat, in reach of a click (first hit at y {bottom:?})"));
            app_now().focus_transcript();
            true
        })),
        ("reader scrolls up", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("reader is up", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(!report().follows && !report().at_end, "Page Up takes the reader into the history");
            held.set(report().offset);
            shot(&out2, "artifacts-91a-before-update");
            // Around the reader: every summary grows a line, the pending
            // decisions are answered, a form's answers go out (Sending…,
            // then accepted), and a new card arrives below.
            let more = "Updated by the agent a moment ago with a longer explanation of what changed and why it matters now.";
            check(control("/__control/artifact-settle", &json!({"session": "art-all", "more": more})).is_ok(), "the Host moves the cards on");
            let added = json!({"session": "art-all", "add": [{"artifact_id": "cd-new", "type": "countdown", "status": "active", "session": "art-all",
                "title": "A countdown made while the reader was away", "target_at": "2026-12-24T18:00:00+01:00", "time_zone": "Europe/Oslo"}]});
            check(control("/__control/artifact-update", &added).is_ok(), "and adds one");
            window.global::<ArtifactBridge>().invoke_open("form-trip-all".into());
            let url = opened().last().cloned().unwrap_or_default();
            let sent = fetch("POST", &format!("{url}/submit"), r#"{"destination":"Oslo"}"#, None);
            check(sent.is_ok_and(|(status, _)| status == 202), "a form's answers go out while the reader is up");
            true
        })),
        ("reader stays put", Box::new(move |_, window, elapsed| {
            let settled = card(window, "dec-deploy-all").is_some_and(|c| c.resolved == "Approved")
                && card(window, "cd-launch-all").is_some_and(|c| c.summary.contains("Updated by the agent"))
                && card(window, "form-trip-all").is_some_and(|c| c.status_text == "Answers accepted")
                && card(window, "cd-new").is_some();
            if !settled || elapsed < Duration::from_millis(1000) {
                if elapsed > Duration::from_secs(6) {
                    check(false, "the cards change: decisions answered, summaries grown, the form accepted, the new card shown");
                    return true;
                }
                return false;
            }
            let moved = (report().offset - held2.get()).abs();
            let after = format!("{out3}/artifacts-91-reader-held.png");
            shot(&out3, "artifacts-91-reader-held");
            let shift = vertical_shift(&format!("{out3}/artifacts-91a-before-update.png"), &after);
            check(!report().follows && moved < 1.0, &format!("a reader scrolled up keeps the offset while cards around change ({moved}px)"));
            check(shift.as_ref().is_ok_and(|s| s.abs() <= 1), &format!("and what they read does not move on screen: {shift:?} px"));
            headless::press(slint::platform::Key::End);
            true
        })),
        ("back to the latest", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            let last = cards(window).last().map(|(_, a)| a.id.to_string()).unwrap_or_default();
            check(report().at_end && report().follows && last == "cd-new", &format!("End returns to the latest, the new card last: {last}"));
            true
        })),
    ]);
    stages
}

pub(super) fn artifacts_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(countdown_stages(&out));
    stages.extend(html_form_stages(&out));
    stages.extend(decision_stages(&out));
    stages.extend(question_stages(&out));
    stages.extend(scroll_stages(&out));
    run_stages(stages);
}
