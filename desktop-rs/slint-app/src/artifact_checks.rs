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
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("decision at the top", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("decision first card", Box::new(move |app, _, elapsed| {
            // Cards report where they are every 150 ms: wait for the first.
            // Home can stop short of the top while the list corrects its
            // estimates (the transcript's): press it again now and then.
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("dec-deploy") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            headless::press("j");
            true
        })),
        ("decision chosen", Box::new(|_, window, elapsed| {
            if selected(window) != "dec-deploy" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("J at the top selects the first decision: {:?}", selected(window)));
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
        ("decision to the latest", Box::new(|_, _, _| {
            app_now().focus_transcript();
            headless::press(slint::platform::Key::End);
            true
        })),
        ("an approval button is clicked", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
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
    let (out, out2, out3) = (out.to_owned(), out.to_owned(), out.to_owned());
    let ids = ["q-trip", "q-custom", "q-picked", "q-written", "q-ranking", "q-escape", "q-click"];
    let tall: Rc<Cell<f32>> = Rc::default();
    let tall2 = tall.clone();
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
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("question at the top", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("question first card", Box::new(move |app, _, elapsed| {
            // Cards report where they are every 150 ms: wait for the first.
            // Home can stop short of the top while the list corrects its
            // estimates (the transcript's): press it again now and then.
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("q-trip") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out3, "artifacts-04a-question-top");
            headless::press("j");
            true
        })),
        ("question chosen", Box::new(|_, window, elapsed| {
            if selected(window) != "q-trip" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("J at the top selects the first question: {:?}", selected(window)));
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
        ("custom answer", Box::new(move |app, window, elapsed| {
            if selected(window) != "q-custom" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("J moves to the next question: {:?} (cursor {:?}, keyboard in {}, field {}) of {:?}", selected(window), app.artifact_cursor.borrow(), crate::commands::context(app, window), window.global::<ArtifactBridge>().get_editing(), crate::artifacts_view::on_screen(app)));
                    true
                };
            }
            tall.set(height_of(app, "q-custom"));
            // Its last number writes an answer of one's own.
            headless::press("3");
            true
        })),
        ("writing", Box::new(|app, window, elapsed| {
            let editing = card(window, "q-custom").is_some_and(|c| c.chosen == 2 && c.editing);
            if !editing || elapsed < Duration::from_millis(300) {
                return elapsed > Duration::from_secs(2) && { check(false, "3 opens the answer of one's own"); true };
            }
            check(crate::commands::context(app, window) == "composer", &format!("the keyboard types into it (letters are not shortcuts): context {}, field focused {}", crate::commands::context(app, window), window.global::<ArtifactBridge>().get_editing()));
            headless::type_text("Friday, after lunch");
            true
        })),
        ("typed", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            let (before, after) = (tall2.get(), height_of(app, "q-custom"));
            check(before > 0.0 && (before - after).abs() < 0.5, &format!("opening the answer of one's own and typing keep the card's height: {before} then {after}"));
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
        ("keyboard back", Box::new(|_, window, elapsed| {
            let back = report().transcript_focused && !window.global::<ArtifactBridge>().get_editing() && card(window, "q-custom").is_some_and(|c| !c.editing && !c.pending);
            if !back && elapsed < Duration::from_secs(1) {
                return false;
            }
            check(back, "sent, the keyboard is back on the chat and the field is gone");
            headless::press(slint::platform::Key::End);
            true
        })),
        ("escape from the field", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(700) {
                return false;
            }
            // From the latest card (q-click), K once more is q-escape.
            headless::press("k");
            headless::press("k");
            true
        })),
        ("escape writing", Box::new(|app, window, elapsed| {
            if selected(window) != "q-escape" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("K reaches the question above the latest: {:?} (cursor {:?}) of {:?}", selected(window), app.artifact_cursor.borrow(), crate::artifacts_view::on_screen(app)));
                    true
                };
            }
            headless::press("3");
            true
        })),
        ("escape typed", Box::new(|app, window, elapsed| {
            let editing = window.global::<ArtifactBridge>().get_editing();
            if !editing {
                return elapsed > Duration::from_secs(2) && { check(false, "3 opens its field"); true };
            }
            headless::type_text("Somewhere warm");
            headless::press(slint::platform::Key::Escape);
            let _ = app;
            true
        })),
        ("escaped", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            let shown = card(window, "q-escape");
            check(report().transcript_focused && crate::commands::context(app, window) == "pane" && !window.global::<ArtifactBridge>().get_editing(),
                "Escape gives the keyboard back to the chat");
            check(shown.as_ref().is_some_and(|c| !c.editing && c.pending && c.draft == "Somewhere warm") && posts("/decisions/q-escape/resolve").is_empty(),
                &format!("the field stops taking the keyboard, keeps the draft and sends nothing: {:?}", shown.map(|c| (c.editing, c.draft))));
            true
        })),
        ("question to the latest", Box::new(|_, _, _| {
            app_now().focus_transcript();
            headless::press(slint::platform::Key::End);
            true
        })),
        ("question clicked", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
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

// ---- plan

fn plan_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["plan-ship", "plan-done", "plan-blocked", "plan-missing", "plan-last"];
    let before: Rc<RefCell<Vec<(String, f32)>>> = Rc::default();
    let before2 = before.clone();
    let mut stages = load_chat("art-plan", &["plan"]);
    stages.extend::<Vec<Stage>>(vec![
        ("plan cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let ship = card(window, "plan-ship").expect("ship");
            // iOS counts items and subtasks completed against the plan's total.
            check(ship.label == "PLAN" && ship.progress_count == "2/5" && (ship.progress_value - 0.4).abs() < 0.01, &format!("a plan's progress: {:?} {}", ship.progress_count, ship.progress_value));
            check(ship.current == "Port the transcript to Slint without changing how it scrolls for a reader who is up", &format!("and the step under way: {:?}", ship.current));
            check(ship.action == "Open plan", &format!("it opens: {:?}", ship.action));
            let done = card(window, "plan-done").expect("done");
            check(done.progress_count == "1/1" && done.progress_value >= 0.99 && done.current.is_empty(), &format!("a finished plan: {:?} {:?}", done.progress_count, done.current));
            let blocked = card(window, "plan-blocked").expect("blocked");
            check(blocked.badge == "Failed" && blocked.failed, &format!("a blocked plan shows as failed: {:?}", blocked.badge));
            check(blocked.current == "Wait for the DBA" && blocked.current_blocked, &format!("and names its blocked step: {:?}", blocked.current));
            let missing = card(window, "plan-missing").expect("missing");
            check(missing.progress_value < 0.0 && missing.current == "Plan details unavailable" && missing.action.is_empty(), &format!("a plan without its details says so: {:?}", missing.current));
            app.focus_transcript();
            true
        })),
        ("plan keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("plan first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("plan-ship") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-05-plan");
            headless::press("j");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("plan opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter opens the plan: overlay {:?}", window.get_overlay())); true };
            }
            check(window.get_report_title() == "Ship the Slint desktop client to the beta group before the end of the quarter", "the plan opens in the report viewer, under its title");
            check(window.get_report_summary() == "Ship 2.0 · Beta testers on the Slint app · 2/5 done", &format!("with its plan, goal and progress: {:?}", window.get_report_summary()));
            // Three items and two subtasks, a row each under the header.
            let rows = window.get_report_blocks().iter().find(|b| b.kind == "table").map(|b| b.rows.row_count()).unwrap_or(0);
            check(rows == 6, &format!("and every item and subtask, a row each: {rows} rows"));
            shot(&out2, "artifacts-05b-plan-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("plan closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty() && report().transcript_focused, "Escape closes it, back on the chat");
            headless::press(slint::platform::Key::End);
            true
        })),
        ("plans move on", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if !shown.contains(&"plan-last".to_owned()) && elapsed < Duration::from_secs(3) {
                return false;
            }
            *before.borrow_mut() = crate::artifacts_view::card_heights(app);
            let more = "";
            check(control("/__control/artifact-settle", &json!({"session": "art-plan", "more": more})).is_ok(), "the Host moves the plans on");
            true
        })),
        ("plans moved on", Box::new(move |app, window, elapsed| {
            let moved = card(window, "plan-ship").is_some_and(|c| c.current == "Write the release notes" && c.progress_count == "4/5")
                && card(window, "plan-last").is_some_and(|c| c.progress_count == "2/2" && c.current.is_empty());
            if !moved || elapsed < Duration::from_millis(800) {
                return elapsed > Duration::from_secs(5) && { check(false, "the plans move on: the next step under way, the last plan done"); true };
            }
            let changed = changed_heights(&before2.borrow(), &crate::artifacts_view::card_heights(app));
            let measured: Vec<String> = before2.borrow().iter().map(|(id, _)| id.clone()).collect();
            check(measured.contains(&"plan-last".to_owned()) && changed.is_empty(), &format!("a plan moving on or finishing keeps its height: {changed:?} of {measured:?}"));
            true
        })),
    ]);
    stages
}

// ---- document

fn document_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["doc-spec", "doc-summary", "doc-huge"];
    let mut stages = load_chat("art-document", &["document"]);
    stages.extend::<Vec<Stage>>(vec![
        ("document cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let spec = card(window, "doc-spec").expect("spec");
            check(spec.label == "DOCUMENT" && spec.action == "Open document", &format!("a document offers to open: {:?} {:?}", spec.label, spec.action));
            check(
                spec.preview.starts_with("Slint client: design notes · The transcript keeps its offset") && !spec.preview.contains('#') && !spec.preview.contains("**") && !spec.preview.contains("fn main"),
                &format!("its first lines as plain text: {:?}", spec.preview),
            );
            let summary = card(window, "doc-summary").expect("summary");
            check(summary.badge == "Draft" && summary.preview.is_empty() && summary.summary.starts_with("Only a summary") && summary.action.is_empty(), &format!("without a body, its summary and nothing to open: {:?} {:?}", summary.preview, summary.action));
            app.focus_transcript();
            true
        })),
        ("document keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::End);
            true
        })),
        ("huge document", Box::new(move |app, _, elapsed| {
            if !crate::artifacts_view::on_screen(app).contains(&"doc-huge".to_owned()) && elapsed < Duration::from_secs(3) {
                return false;
            }
            shot(&out, "artifacts-06-document");
            headless::press("k");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("document opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter opens the document: overlay {:?}", window.get_overlay())); true };
            }
            let blocks = window.get_report_blocks().row_count();
            check(window.get_report_title() == "A very long report" && blocks >= 200, &format!("the whole document in the report viewer: {:?}, {blocks} blocks", window.get_report_title()));
            shot(&out2, "artifacts-06b-document-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("document closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty(), "Escape closes it");
            true
        })),
    ]);
    stages
}

// ---- research

fn research_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["res-market", "res-html", "res-plain"];
    let mut stages = load_chat("art-research", &["research"]);
    stages.extend::<Vec<Stage>>(vec![
        ("research cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let market = card(window, "res-market").expect("market");
            check(market.label == "RESEARCH" && market.action == "Open research" && market.preview.starts_with("Findings · Most clients ship Electron"), &format!("research shows its first lines and opens: {:?} {:?}", market.action, market.preview));
            // iOS lists only https sources; the count says how many open.
            check(market.sources == "2 sources", &format!("and how many sources it cites: {:?}", market.sources));
            let html = card(window, "res-html").expect("html");
            check(html.preview.starts_with("Market · Growth is 12% a year") && !html.preview.contains("steal"), &format!("an HTML body reads as its text, scripts dropped: {:?}", html.preview));
            check(card(window, "res-plain").is_some_and(|c| c.sources.is_empty()), "no sources, no sources line");
            let detail = crate::artifacts_view::detail(app, "res-market").map(|d| clarp_core::json::string(&d, "body")).unwrap_or_default();
            check(
                detail.contains("Most clients ship") && detail.contains("[Gartner forecast](https://gartner.example/r)") && detail.contains("[Vendor blog](https://vendor.example/b)") && !detail.contains("insecure.example"),
                &format!("opened, its body and its https sources as links: {detail:?}"),
            );
            app.focus_transcript();
            true
        })),
        ("research keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("research first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("res-market") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-07-research");
            headless::press("j");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("research opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter opens the research: overlay {:?}", window.get_overlay())); true };
            }
            check(window.get_report_title().starts_with("Desktop agent clients in 2026") && window.get_report_kind() == "RESEARCH", &format!("in the report viewer, as research: {:?}", window.get_report_kind()));
            shot(&out2, "artifacts-07b-research-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("research closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty(), "Escape closes it");
            true
        })),
    ]);
    stages
}

// ---- code_change

fn code_change_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["cc-big", "cc-bare", "cc-failed"];
    let mut stages = load_chat("art-code", &["code_change"]);
    stages.extend::<Vec<Stage>>(vec![
        ("code cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let big = card(window, "cc-big").expect("big");
            check(big.label == "CODE CHANGE" && big.repo == "clarp · slint-artifacts" && big.action == "Open change", &format!("a change says where: {:?} {:?}", big.repo, big.action));
            check(big.files == "12 files" && big.additions == "+840" && big.deletions == "−132", &format!("and how much: {:?} {:?} {:?}", big.files, big.additions, big.deletions));
            let bare = card(window, "cc-bare").expect("bare");
            check(bare.repo == "Repository" && bare.files.is_empty() && bare.additions.is_empty() && bare.action.is_empty(), &format!("one without details names no repository and opens nothing: {:?} {:?}", bare.repo, bare.action));
            let failed = card(window, "cc-failed").expect("failed");
            check(failed.badge == "Failed" && failed.files == "1 file" && failed.additions == "+0" && failed.deletions == "−3", &format!("a failed one keeps its numbers: {:?} {:?}", failed.badge, failed.files));
            app.focus_transcript();
            true
        })),
        ("code keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("code first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("cc-big") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-08-code-change");
            headless::press("j");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("code opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter opens the change: overlay {:?}", window.get_overlay())); true };
            }
            let blocks: Vec<crate::MessageBlock> = window.get_report_blocks().iter().collect();
            let diff = blocks.iter().find(|b| b.kind == "code").map(|b| b.text.to_string()).unwrap_or_default();
            check(window.get_report_kind() == "CODE CHANGE" && window.get_report_summary() == "clarp · slint-artifacts · 4560a8ed", &format!("the change, where and which commit: {:?} {:?}", window.get_report_kind(), window.get_report_summary()));
            check(diff.contains("-fn main() {}") && diff.contains("+    println!(\"cards\");"), &format!("with its diff: {diff:?}"));
            check(blocks.len() >= 2, "and a link to its source");
            shot(&out2, "artifacts-08b-code-change-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("code closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty(), "Escape closes it");
            true
        })),
    ]);
    stages
}

// ---- data

fn data_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["data-sales", "data-wide", "data-empty", "data-broken"];
    let strings = |m: slint::ModelRc<slint::SharedString>| m.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let mut stages = load_chat("art-data", &["data"]);
    stages.extend::<Vec<Stage>>(vec![
        ("data cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let sales = card(window, "data-sales").expect("sales");
            check(sales.label == "DATA" && sales.action == "Open table" && sales.rows_count == "120 rows", &format!("a table, its rows counted: {:?} {:?}", sales.action, sales.rows_count));
            // iOS: the header and the first row; true reads Yes, null —.
            check(strings(sales.head.clone()) == ["Region", "Q1", "Q2", "Q3", "Q4", "Audited", "Note"], &format!("its header: {:?}", strings(sales.head.clone())));
            check(strings(sales.first_row.clone()) == ["Nordics 0", "100", "120", "90", "150", "Yes", "—"], &format!("and its first row: {:?}", strings(sales.first_row.clone())));
            let wide = card(window, "data-wide").expect("wide");
            let head = strings(wide.head.clone());
            check(head.len() == 7 && head[6] == "+24", &format!("thirty columns show six and how many more: {head:?}"));
            let empty = card(window, "data-empty").expect("empty");
            check(empty.rows_count == "0 rows" && empty.first_row.row_count() == 0, &format!("an empty table: {:?}", empty.rows_count));
            let broken = card(window, "data-broken").expect("broken");
            check(broken.data_note == "Structured data unavailable" && broken.head.row_count() == 0 && broken.action.is_empty(), &format!("no columns, no table: {:?}", broken.data_note));
            app.focus_transcript();
            true
        })),
        ("data keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("data first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("data-sales") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-09-data");
            headless::press("j");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("data opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter opens the table: overlay {:?}", window.get_overlay())); true };
            }
            let tables: Vec<usize> = window.get_report_blocks().iter().filter(|b| b.kind == "table").map(|b| b.rows.row_count()).collect();
            // The chart (bar: a row per category) and the grid (header and 120 rows).
            check(window.get_report_kind() == "DATA" && tables.contains(&121), &format!("the whole table in the report viewer: {tables:?}"));
            // The chart's first 30 categories as bars, then the grid.
            check(tables == [31, 121], &format!("with its chart first, a bar per category: {tables:?}"));
            shot(&out2, "artifacts-09b-data-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("data closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty(), "Escape closes it");
            true
        })),
    ]);
    stages
}

// ---- audio

fn audio_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let ids = ["aud-brief", "aud-gone", "aud-elsewhere"];
    let mut stages = load_chat("art-audio", &["audio"]);
    stages.extend::<Vec<Stage>>(vec![
        ("audio cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let brief = card(window, "aud-brief").expect("brief");
            // iOS titles only a file card with its file's name.
            check(brief.label == "AUDIO" && brief.title.starts_with("This morning's stand-up") && brief.action == "Play", &format!("an audio card keeps its title and offers to play: {:?} {:?}", brief.title, brief.action));
            check(brief.media_state == "idle" && brief.media_length == "1:12", &format!("idle, with its length: {:?} {:?}", brief.media_state, brief.media_length));
            let elsewhere = card(window, "aud-elsewhere").expect("elsewhere");
            check(elsewhere.media_text == "Audio unavailable" && elsewhere.action.is_empty(), &format!("audio not on the Host cannot play: {:?}", elsewhere.media_text));
            app.focus_transcript();
            true
        })),
        ("audio keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("audio first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("aud-brief") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            headless::press("j");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("audio plays", Box::new(move |_, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "playing" || elapsed < Duration::from_millis(1600) {
                return elapsed > Duration::from_secs(4) && { check(false, &format!("Enter plays it: {:?} {:?}", brief.media_state, brief.media_text)); true };
            }
            let fetched = super::requests("GET", "/media/aud1");
            check(fetched.last().is_some_and(|r| r["authorization"] == "Bearer probe-token"), "the clip comes from the Host with the app's token");
            check(brief.media_text == "Playing" && brief.action == "Pause", &format!("it says it plays and offers to pause: {:?} {:?}", brief.media_text, brief.action));
            let bridge = window.global::<ArtifactBridge>();
            let position = bridge.invoke_media_position(brief.media_played, brief.media_started, bridge.get_now());
            check(position.as_str() != "0:00" && position.starts_with("0:0"), &format!("its position moves: {position}"));
            shot(&out, "artifacts-10-audio");
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("audio paused", Box::new(|_, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "paused" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("Enter again pauses it: {:?}", brief.media_state)); true };
            }
            check(brief.media_text == "Paused" && brief.action == "Play" && brief.media_played >= 1, &format!("paused where it was: {:?} {}s", brief.media_text, brief.media_played));
            headless::press(slint::platform::Key::Return);
            true
        })),
        ("audio resumes", Box::new(|_, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "playing" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("and again resumes it: {:?}", brief.media_state)); true };
            }
            check(true, "and again resumes it");
            true
        })),
        ("audio ends", Box::new(|_, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "idle" {
                return elapsed > Duration::from_secs(8) && { check(false, &format!("the clip ends: {:?}", brief.media_state)); true };
            }
            check(brief.action == "Play", "at its end it can play again");
            window.global::<ArtifactBridge>().invoke_open("aud-gone".into());
            true
        })),
        ("audio gone", Box::new(|_, window, elapsed| {
            let gone = card(window, "aud-gone").expect("gone");
            if gone.media_state != "failed" {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("an expired clip fails: {:?}", gone.media_state)); true };
            }
            check(gone.media_text == "Couldn't prepare audio" && gone.action == "Play", &format!("an expired clip says it could not be prepared, and can be tried again: {:?}", gone.media_text));
            true
        })),
    ]);
    stages
}

// ---- a chat full of artifacts

/// Every type so far, the one ending on a clickable card last.
const ALL_TYPES: &[&str] = &["countdown", "decision", "question", "plan", "document", "research", "code_change", "data", "audio", "html_form"];

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

/// A card's height as it last reported it (0 when it is not on screen).
fn height_of(app: &crate::App, id: &str) -> f32 {
    crate::artifacts_view::card_heights(app).into_iter().find(|(card, _)| card == id).map_or(0.0, |(_, h)| h)
}

/// The cards in both lists whose heights differ.
fn changed_heights(before: &[(String, f32)], after: &[(String, f32)]) -> Vec<(String, f32, f32)> {
    before
        .iter()
        .filter_map(|(id, h)| after.iter().find(|(other, _)| other == id).filter(|(_, a)| (a - h).abs() >= 0.5).map(|(_, a)| (id.clone(), *h, *a)))
        .collect()
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
    let heights: Rc<RefCell<Vec<(String, f32)>>> = Rc::default();
    let heights2 = heights.clone();
    let at_end: Rc<RefCell<Vec<(String, f32)>>> = Rc::default();
    let at_end2 = at_end.clone();
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
        ("keyboard on screen", Box::new(|app, window, _| {
            if !report().transcript_focused {
                return false;
            }
            app.artifact_cursor.borrow_mut().clear();
            headless::press("k");
            let shown = crate::artifacts_view::on_screen(app);
            check(!shown.is_empty() && selected(window) == "form-stale-all" && shown.contains(&selected(window)), &format!("K selects the lowest card on screen: {:?} of {shown:?}", selected(window)));
            for _ in 0..40 {
                headless::press("k");
            }
            true
        })),
        ("K at the top of the screen", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let shown = crate::artifacts_view::on_screen(app);
            check(shown.first() == Some(&selected(window)), &format!("K never leaves the screen: it stops at the topmost card shown {:?} of {shown:?}", selected(window)));
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("selection off screen", Box::new(|app, window, elapsed| {
            // A card the list dropped lapses 600 ms after its last report.
            if elapsed < Duration::from_millis(1200) {
                return false;
            }
            let before = (opened().len(), window.get_overlay().to_string());
            let gone = !crate::artifacts_view::on_screen(app).contains(&app.artifact_cursor.borrow());
            headless::press(slint::platform::Key::Return);
            let after = (opened().len(), window.get_overlay().to_string());
            check(gone && crate::artifacts_view::selected(app).is_none() && before == after, &format!("a selected card scrolled off screen is not acted on: off {gone}, {before:?} then {after:?}"));
            check(selected(window).is_empty(), "and shows no selection");
            headless::press("k");
            let shown = crate::artifacts_view::on_screen(app);
            check(shown.last() == Some(&selected(window)), &format!("K then picks the lowest card now on screen: {:?} of {shown:?}", selected(window)));
            app.artifact_cursor.borrow_mut().clear();
            true
        })),
        ("reader scrolls up", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            true
        })),
        ("reader is up", Box::new(move |app, window, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(!report().follows && !report().at_end, "Page Up takes the reader into the history");
            held.set(report().offset);
            *heights.borrow_mut() = crate::artifacts_view::card_heights(app);
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
        ("reader stays put", Box::new(move |app, window, elapsed| {
            let settled = card(window, "dec-deploy-all").is_some_and(|c| c.resolved == "Approved")
                && card(window, "plan-ship-all").is_some_and(|c| c.current == "Write the release notes")
                && card(window, "plan-last-all").is_some_and(|c| c.progress_count == "2/2" && c.current.is_empty())
                && card(window, "cd-launch-all").is_some_and(|c| c.summary.contains("Updated by the agent"))
                && card(window, "form-trip-all").is_some_and(|c| c.status_text == "Answers accepted")
                && card(window, "cd-new").is_some();
            if !settled || elapsed < Duration::from_millis(1000) {
                if elapsed > Duration::from_secs(6) {
                    check(false, "the cards change: decisions answered, plans moved on, summaries grown, the form accepted, the new card shown");
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
            let changed = changed_heights(&heights2.borrow(), &crate::artifacts_view::card_heights(app));
            check(!heights2.borrow().is_empty() && changed.is_empty(), &format!("no card on screen changed height as it changed state: {changed:?}"));
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
        ("scrolling keeps heights", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            *at_end.borrow_mut() = crate::artifacts_view::card_heights(app);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("paged up and back", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            headless::press(slint::platform::Key::PageDown);
            true
        })),
        ("heights after scrolling", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            let changed = changed_heights(&at_end2.borrow(), &crate::artifacts_view::card_heights(app));
            check(!at_end2.borrow().is_empty() && changed.is_empty(), &format!("scrolling away and back changes no card's height: {changed:?}"));
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
    stages.extend(plan_stages(&out));
    stages.extend(document_stages(&out));
    stages.extend(research_stages(&out));
    stages.extend(code_change_stages(&out));
    stages.extend(data_stages(&out));
    stages.extend(audio_stages(&out));
    stages.extend(scroll_stages(&out));
    run_stages(stages);
}
