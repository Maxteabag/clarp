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

pub(super) fn artifacts_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(countdown_stages(&out));
    stages.extend(html_form_stages(&out));
    run_stages(stages);
}
