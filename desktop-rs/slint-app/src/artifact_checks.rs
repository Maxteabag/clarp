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
            headless::press("o");
            true
        })),
        ("form opens in the browser", Box::new(move |_, _, elapsed| {
            let Some(url) = opened().into_iter().find(|u| u.contains("/form/")) else {
                if elapsed > Duration::from_secs(3) {
                    check(false, &format!("O opens the form in the browser: {:?}", opened()));
                    return true;
                }
                return false;
            };
            check(url.starts_with("http://127.0.0.1:"), &format!("O opens the form from a loopback page: {url}"));
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
            headless::press("1");
            true
        })),
        ("decision sent", Box::new(|_, window, elapsed| {
            let sent = posts("/decisions/d-deploy/resolve");
            let shown = card(window, "dec-deploy").is_some_and(|c| !c.pending && c.resolved == "Approved");
            if (sent.is_empty() || !shown) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let body = sent.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body == json!({"choice": "accepted", "expected_revision": 4}), &format!("1 again approves against the revision seen: {body}"));
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
            headless::press("2");
            true
        })),
        ("question answered", Box::new(|_, window, elapsed| {
            let sent = posts("/decisions/q-trip/resolve");
            let shown = card(window, "q-trip").is_some_and(|c| c.resolved == "Answer saved" && c.answer == "Tromsø");
            if (sent.is_empty() || !shown) && elapsed < Duration::from_secs(4) {
                return false;
            }
            let body = sent.last().map(|e| e["body"].clone()).unwrap_or(Value::Null);
            check(body == json!({"answer": {"option_id": "tromso"}, "expected_revision": 7}), &format!("2 again sends the chosen option: {body}"));
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
            headless::press("o");
            true
        })),
        ("plan opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the plan: overlay {:?}", window.get_overlay())); true };
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
            // The viewer had the keyboard: the chat gets it back a moment later.
            if (!window.get_overlay().is_empty() || !report().transcript_focused) && elapsed < Duration::from_secs(2) {
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
            headless::press("o");
            true
        })),
        ("document opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the document: overlay {:?}", window.get_overlay())); true };
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
            headless::press("o");
            true
        })),
        ("research opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the research: overlay {:?}", window.get_overlay())); true };
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
            headless::press("o");
            true
        })),
        ("code opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the change: overlay {:?}", window.get_overlay())); true };
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
            headless::press("o");
            true
        })),
        ("data opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the table: overlay {:?}", window.get_overlay())); true };
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
    let (out_idle, out_end) = (out.clone(), out.clone());
    // The clip's height while idle, which playing, pausing and ending keep.
    let idle: Rc<Cell<f32>> = Rc::default();
    let (idle2, idle3, idle4) = (idle.clone(), idle.clone(), idle.clone());
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
            idle.set(height_of(app, "aud-brief"));
            shot(&out_idle, "artifacts-10a-audio-idle");
            headless::press("j");
            headless::press("o");
            true
        })),
        ("audio plays", Box::new(move |app, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "playing" || elapsed < Duration::from_millis(1600) {
                return elapsed > Duration::from_secs(4) && { check(false, &format!("O plays it: {:?} {:?}", brief.media_state, brief.media_text)); true };
            }
            let fetched = super::requests("GET", "/media/aud1");
            check(fetched.last().is_some_and(|r| r["authorization"] == "Bearer probe-token"), "the clip comes from the Host with the app's token");
            check(brief.media_text == "Playing" && brief.action == "Pause", &format!("it says it plays and offers to pause: {:?} {:?}", brief.media_text, brief.action));
            let bridge = window.global::<ArtifactBridge>();
            let position = bridge.invoke_media_position(brief.media_played, brief.media_started, bridge.get_now());
            check(position.as_str() != "0:00" && position.starts_with("0:0"), &format!("its position moves: {position}"));
            let (before, now) = (idle2.get(), height_of(app, "aud-brief"));
            check(before > 0.0 && (before - now).abs() < 0.5, &format!("playing keeps the card's height: {before} then {now}"));
            shot(&out, "artifacts-10-audio");
            headless::press("o");
            true
        })),
        ("audio paused", Box::new(move |app, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "paused" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O again pauses it: {:?}", brief.media_state)); true };
            }
            check(brief.media_text == "Paused" && brief.action == "Play" && brief.media_played >= 1, &format!("paused where it was: {:?} {}s", brief.media_text, brief.media_played));
            let (before, now) = (idle3.get(), height_of(app, "aud-brief"));
            check((before - now).abs() < 0.5, &format!("pausing keeps it: {before} then {now}"));
            headless::press("o");
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
        ("audio ends", Box::new(move |app, window, elapsed| {
            let brief = card(window, "aud-brief").expect("brief");
            if brief.media_state != "idle" {
                return elapsed > Duration::from_secs(8) && { check(false, &format!("the clip ends: {:?}", brief.media_state)); true };
            }
            check(brief.action == "Play", "at its end it can play again");
            let (before, now) = (idle4.get(), height_of(app, "aud-brief"));
            check((before - now).abs() < 0.5, &format!("and the card is as tall as before it played: {before} then {now}"));
            let end = format!("{out_end}/artifacts-10c-audio-ended.png");
            shot(&out_end, "artifacts-10c-audio-ended");
            let shift = vertical_shift(&format!("{out_end}/artifacts-10a-audio-idle.png"), &end);
            check(shift.as_ref().is_ok_and(|s| s.abs() <= 1), &format!("nothing in the chat moved while it played: {shift:?} px"));
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

// ---- video

fn video_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let ids = ["vid-demo", "vid-plain", "vid-gone", "vid-none"];
    let first: Rc<Cell<f32>> = Rc::default();
    let first2 = first.clone();
    let mut stages = load_chat("art-video", &["video"]);
    stages.extend::<Vec<Stage>>(vec![
        ("video cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let demo = card(window, "vid-demo").expect("demo");
            check(demo.label == "VIDEO" && demo.action == "Play video" && demo.media_length == "1:35", &format!("a video offers to play, with its length: {:?} {:?}", demo.action, demo.media_length));
            check(!demo.has_poster, "its poster is still on its way (the Host is slow)");
            let none = card(window, "vid-none").expect("none");
            check(none.media_text == "Video unavailable" && none.action.is_empty(), &format!("no url, nothing to play: {:?}", none.media_text));
            app.focus_transcript();
            true
        })),
        ("video keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("video first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("vid-demo") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            first.set(height_of(app, "vid-demo"));
            true
        })),
        ("poster lands", Box::new(move |app, window, elapsed| {
            let demo = card(window, "vid-demo").expect("demo");
            if !demo.has_poster || elapsed < Duration::from_millis(400) {
                return elapsed > Duration::from_secs(5) && { check(false, "the poster arrives from the Host"); true };
            }
            let (before, after) = (first2.get(), height_of(app, "vid-demo"));
            check(demo.poster.size().width == 320 && before > 0.0 && (before - after).abs() < 0.5, &format!("the poster lands in the space kept for it: {before} then {after}"));
            check(card(window, "vid-plain").is_some_and(|c| !c.has_poster), "a video without a poster keeps its placeholder");
            shot(&out, "artifacts-11-video");
            headless::press("j");
            headless::press("o");
            true
        })),
        ("video opens", Box::new(|_, window, elapsed| {
            let found = opened().into_iter().find(|u| u.starts_with("file://") && u.ends_with("cards-demo.mp4"));
            let Some(url) = found else {
                return elapsed > Duration::from_secs(4) && { check(false, &format!("O opens the video in the system's player: {:?}", opened())); true };
            };
            let fetched = super::requests("GET", "/media/vid1");
            check(fetched.last().is_some_and(|r| r["authorization"] == "Bearer probe-token"), "the video comes from the Host with the app's token");
            let path = url.trim_start_matches("file://").to_owned();
            check(std::fs::read(&path).is_ok_and(|b| b.ends_with(b"fixture-video")), &format!("saved whole where it opened: {path}"));
            check(card(window, "vid-demo").is_some_and(|c| c.status_text == "Opened in your video player"), "the card says where it went");
            window.global::<ArtifactBridge>().invoke_open("vid-gone".into());
            true
        })),
        ("video gone", Box::new(|_, window, elapsed| {
            let gone = card(window, "vid-gone").map(|c| c.status_text.to_string()).unwrap_or_default();
            if !gone.starts_with("Couldn't download") {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("an expired video says so: {gone:?}")); true };
            }
            check(gone.contains("404"), &format!("an expired video says why, and O tries again: {gone:?}"));
            true
        })),
    ]);
    stages
}

// ---- file

fn file_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let ids = ["file-pdf", "file-csv", "file-gone", "file-elsewhere"];
    let mut stages = load_chat("art-file", &["file"]);
    stages.extend::<Vec<Stage>>(vec![
        ("file cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            // iOS titles a file card with its file's name.
            let pdf = card(window, "file-pdf").expect("pdf");
            check(pdf.label == "FILE" && pdf.title == "contract-2026-signed.pdf" && pdf.file_info == "PDF · 1.2 MB" && pdf.action == "Open file", &format!("a file, named, its kind and size: {:?} {:?} {:?}", pdf.title, pdf.file_info, pdf.action));
            check(card(window, "file-csv").is_some_and(|c| c.file_info == "CSV · 2.0 KB"), "a small one in KB");
            let elsewhere = card(window, "file-elsewhere").expect("elsewhere");
            check(elsewhere.action.is_empty() && elsewhere.file_info.starts_with("File unavailable"), &format!("a file not on the Host cannot open: {:?}", elsewhere.file_info));
            app.focus_transcript();
            true
        })),
        ("file keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("file first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("file-pdf") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            headless::press("j");
            headless::press("o");
            true
        })),
        ("file opens", Box::new(move |_, window, elapsed| {
            let found = opened().into_iter().find(|u| u.starts_with("file://") && u.ends_with("contract-2026-signed.pdf"));
            let Some(url) = found else {
                return elapsed > Duration::from_secs(4) && { check(false, &format!("O opens the file with the desktop's app: {:?}", opened())); true };
            };
            let fetched = super::requests("GET", "/media/pdf1");
            check(fetched.last().is_some_and(|r| r["authorization"] == "Bearer probe-token"), "the file comes from the Host with the app's token");
            check(std::fs::read(url.trim_start_matches("file://")).is_ok_and(|b| b.starts_with(b"%PDF")), "saved whole under its own name");
            check(card(window, "file-pdf").is_some_and(|c| c.status_text == "Opened"), "the card says it opened");
            shot(&out, "artifacts-12-file");
            window.global::<ArtifactBridge>().invoke_open("file-gone".into());
            true
        })),
        ("file gone", Box::new(|_, window, elapsed| {
            let gone = card(window, "file-gone").map(|c| c.status_text.to_string()).unwrap_or_default();
            if !gone.starts_with("Couldn't download") {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("an expired file says so: {gone:?}")); true };
            }
            check(gone.contains("404"), &format!("an expired file says why, and O tries again: {gone:?}"));
            true
        })),
    ]);
    stages
}

// ---- release

fn release_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let ids = ["rel-ready", "rel-failed", "rel-active"];
    let mut stages = load_chat("art-release", &["release"]);
    stages.extend::<Vec<Stage>>(vec![
        ("release cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let ready = card(window, "rel-ready").expect("ready");
            check(ready.label == "RELEASE" && ready.revision == "2.4.0" && ready.release_state == "ready" && ready.action == "Open release", &format!("a release, its version and state: {:?} {:?} {:?}", ready.revision, ready.release_state, ready.action));
            let failed = card(window, "rel-failed").expect("failed");
            check(failed.revision == "deadbeef" && failed.release_state == "failed", &format!("without a version, its commit: {:?} {:?}", failed.revision, failed.release_state));
            let active = card(window, "rel-active").expect("active");
            check(active.revision == "Unknown revision" && active.release_state == "active", &format!("without either, iOS's Unknown revision: {:?}", active.revision));
            app.focus_transcript();
            true
        })),
        ("release keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("release first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("rel-ready") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-13-release");
            headless::press("j");
            headless::press("o");
            true
        })),
        ("release opens", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("O opens the release: overlay {:?}", window.get_overlay())); true };
            }
            let blocks: Vec<crate::MessageBlock> = window.get_report_blocks().iter().collect();
            // iOS's detail: version, build, revision, repository, then the notes and a link.
            let facts = blocks.iter().find(|b| b.kind == "table").map(|b| b.rows.row_count()).unwrap_or(0);
            check(window.get_report_kind() == "RELEASE" && facts == 5, &format!("the release in the report viewer, its facts in a table: {:?} {facts} rows", window.get_report_kind()));
            let body = crate::artifacts_view::detail(&app_now(), "rel-ready").map(|d| clarp_core::json::string(&d, "body")).unwrap_or_default();
            check(blocks.iter().any(|b| b.kind == "heading") && body.contains("Every artifact type embedded") && body.contains("[Open source](https://github.com/example/clarp/releases/2.4.0)"), &format!("with its notes and a link to its source: {body:?}"));
            shot(&out2, "artifacts-13b-release-open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("release closed", Box::new(|_, window, elapsed| {
            if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay().is_empty(), "Escape closes it");
            true
        })),
    ]);
    stages
}

// ---- directory

fn directory_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let ids = ["dir-out", "dir-home", "dir-escape", "dir-link"];
    // The agent works in the check's scratch folder, which has build/out.
    let folder = std::env::var("CLARP_TEST_HOST_LOG").ok().and_then(|l| std::path::Path::new(&l).parent().map(|p| p.to_path_buf())).unwrap_or_default();
    let made = std::fs::create_dir_all(folder.join("build/out"));
    // A link inside the agent's folder to a folder outside it.
    let outside = std::fs::create_dir_all(folder.join("elsewhere")).and_then(|()| std::os::unix::fs::symlink("/usr", folder.join("build/outside")));
    let workspace = folder.to_string_lossy().into_owned();
    let workspace2 = workspace.clone();
    let mut stages: Vec<Stage> = vec![
        ("load", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(made.is_ok() && outside.is_ok(), "the agent's folder has build/out, and a link out of it");
            let loaded = control("/__control/artifact-chat", &json!({"session": "art-directory", "types": ["directory"], "cwd": workspace}));
            check(loaded.is_ok(), "the Host takes a chat of directories");
            // Its folders open only when this desktop shares the Host's files.
            if !app.engine.borrow().shared_filesystem() {
                app.engine.borrow_mut().set_shared_filesystem(true);
            }
            true
        })),
        ("open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("art-directory").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("art-directory");
            crate::pump();
            true
        })),
    ];
    stages.extend::<Vec<Stage>>(vec![
        ("directory cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let dir = card(window, "dir-out").expect("out");
            check(dir.label == "DIRECTORY" && dir.repo == "build/out" && dir.action == "Open folder", &format!("a folder, its path, and it opens: {:?} {:?}", dir.repo, dir.action));
            let escape = card(window, "dir-escape").expect("escape");
            check(escape.action.is_empty() && escape.status_text.is_empty() && escape.file_info == "Folder unavailable", &format!("a path out of its root never opens: {:?} {:?}", escape.action, escape.file_info));
            app.focus_transcript();
            true
        })),
        ("directory keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("directory first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("dir-out") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-14-directory");
            headless::press("j");
            headless::press("o");
            true
        })),
        ("directory opens", Box::new(move |app, window, elapsed| {
            let expected = std::fs::canonicalize(std::path::Path::new(&workspace2).join("build/out")).map(|p| format!("file://{}", p.display())).unwrap_or_default();
            if !opened().contains(&expected) {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("O opens the folder in the file manager: want {expected}, got {:?}", opened())); true };
            }
            check(true, "O opens the folder in the file manager");
            // A link that leads out of the agent's folder is not followed.
            let before = opened().len();
            window.global::<ArtifactBridge>().invoke_open("dir-link".into());
            check(opened().len() == before && !opened().iter().any(|u| u == "file:///usr"), &format!("a link out of the agent's folder does not open: {:?}", opened().last()));
            // Not shared: the folder is on the Host, and the card says so.
            app.engine.borrow_mut().set_shared_filesystem(false);
            window.global::<ArtifactBridge>().invoke_open("dir-out".into());
            true
        })),
        ("directory remote", Box::new(|_, window, elapsed| {
            let said = card(window, "dir-out").map(|c| c.status_text.to_string()).unwrap_or_default();
            if !said.starts_with("On the Host") {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("a Host that does not share its files: the card says where the folder is: {said:?}")); true };
            }
            check(said.contains("build/out"), &format!("a Host that does not share its files: the card says where the folder is: {said:?}"));
            true
        })),
    ]);
    stages
}

// ---- workflow_run

fn workflow_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let ids = ["wf-ci", "wf-queued", "wf-done", "wf-failed"];
    let mut stages = load_chat("art-workflow", &["workflow_run"]);
    stages.extend::<Vec<Stage>>(vec![
        ("workflow cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &ids, elapsed) {
                return false;
            }
            let ci = card(window, "wf-ci").expect("ci");
            check(ci.label == "WORKFLOW RUN" && ci.progress_count == "3/8" && (ci.progress_value - 0.375).abs() < 0.01, &format!("a run's progress: {:?} {}", ci.progress_count, ci.progress_value));
            check(ci.current.starts_with("Run the headless checks") && ci.action == "Open in GitHub" && ci.repo == "CI · run 901", &format!("its step, workflow and run, and it opens: {:?} {:?} {:?}", ci.current, ci.repo, ci.action));
            let queued = card(window, "wf-queued").expect("queued");
            check(queued.progress_value < -1.5 && queued.progress_count.is_empty(), &format!("under way without a count: indeterminate {}", queued.progress_value));
            let failed = card(window, "wf-failed").expect("failed");
            check(failed.failed && failed.outcome == "failure", &format!("a failed run is marked: {:?}", failed.outcome));
            app.focus_transcript();
            true
        })),
        ("workflow keyboard", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("workflow first card", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if shown.first().map(String::as_str) != Some("wf-ci") && elapsed < Duration::from_secs(3) {
                if elapsed.as_millis() % 500 < 100 {
                    headless::press(slint::platform::Key::Home);
                }
                return false;
            }
            shot(&out, "artifacts-15-workflow");
            headless::press("j");
            headless::press("o");
            true
        })),
        ("workflow opens", Box::new(|_, _, elapsed| {
            let url = "https://github.com/example/clarp/actions/runs/901".to_owned();
            if !opened().contains(&url) {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("O opens the run on GitHub: {:?}", opened())); true };
            }
            check(true, "O opens the run on GitHub");
            true
        })),
    ]);
    stages
}

// ---- images in messages

/// The image blocks of a row: (gallery, [(alt, loaded, failed, width)]).
fn image_blocks(window: &crate::AppWindow, id: &str) -> Vec<(bool, Vec<(String, bool, bool, u32)>)> {
    let Some(row) = rows(window).into_iter().find(|r| r.id == id) else { return Vec::new() };
    row.blocks
        .iter()
        .filter(|b| b.kind == "images")
        .map(|b| (b.gallery, b.images.iter().map(|i| (i.alt.to_string(), i.loaded, i.failed, i.image.size().width)).collect()))
        .collect()
}

fn image_stages(out: &str) -> Vec<Stage> {
    let (out_before, out_after) = (out.to_owned(), out.to_owned());
    let turns = json!({"session": "art-images", "turns": [
        {"id": "im-ask", "role": "user", "text": "Show me the charts"},
        {"id": "im-one", "role": "assistant", "text": "Here is the chart:\n\n![Sales by region](clarp-media://asset/img-chart)\n\nAs you can see, Nordics lead."},
        {"id": "im-gallery", "role": "assistant", "text": "And the rest:\n\n```clarp-gallery\n![Q1](clarp-media://asset/img-a)\n![Q2](/media/img-b)\n![Q3](media/img-c)\n![Q4 (missing)](clarp-media://asset/img-gone)\n```"},
        {"id": "im-away", "role": "assistant", "text": "One from the web:\n\n![A tracker](https://tracker.example/p.png)"},
        {"id": "im-slow", "role": "assistant", "text": "The last one is large:\n\n![Slow chart](clarp-media://asset/img-slow)"},
    ]});
    let mut stages = load_chat("art-images", &[]);
    stages.extend::<Vec<Stage>>(vec![
        ("image turns", Box::new(move |_, _, _| {
            check(control("/__control/turns", &turns).is_ok(), "the Host takes replies with images");
            true
        })),
        ("images placed", Box::new(move |_, window, elapsed| {
            let slow = image_blocks(window, "im-slow");
            if slow.is_empty() || elapsed < Duration::from_millis(300) {
                return elapsed > Duration::from_secs(4) && { check(false, "a reply's image shows as an image block"); true };
            }
            // The slow one is still on its way: its space is already kept.
            check(slow[0].1.first().is_some_and(|(alt, loaded, failed, _)| alt == "Slow chart" && !loaded && !failed), &format!("an image on its way keeps its caption, not yet loaded: {slow:?}"));
            shot(&out_before, "artifacts-16a-images-loading");
            true
        })),
        ("images load", Box::new(move |_, window, elapsed| {
            let slow = image_blocks(window, "im-slow");
            let loaded = slow.first().is_some_and(|b| b.1.first().is_some_and(|i| i.1));
            if !loaded || elapsed < Duration::from_millis(500) {
                return elapsed > Duration::from_secs(6) && { check(false, "the slow image arrives"); true };
            }
            let one = image_blocks(window, "im-one");
            check(one.len() == 1 && !one[0].0 && one[0].1 == [("Sales by region".to_owned(), true, false, 400)], &format!("a single image, fetched from the Host: {one:?}"));
            let gallery = image_blocks(window, "im-gallery");
            let tiles: Vec<(String, bool, bool)> = gallery.first().map(|g| g.1.iter().map(|(a, l, f, _)| (a.clone(), *l, *f)).collect()).unwrap_or_default();
            check(gallery.first().is_some_and(|g| g.0) && tiles == [("Q1".into(), true, false), ("Q2".into(), true, false), ("Q3".into(), true, false), ("Q4 (missing)".into(), false, true)],
                &format!("a gallery of four, the missing one marked: {tiles:?}"));
            let away = image_blocks(window, "im-away");
            check(away.first().is_some_and(|b| b.1.first().is_some_and(|i| i.2 && !i.1)), &format!("an image not on the Host is not fetched: {away:?}"));
            check(super::requests("GET", "/media/img-chart").last().is_some_and(|r| r["authorization"] == "Bearer probe-token"), "images come from the Host with the app's token");
            check(super::requests("GET", "/p.png").is_empty(), "and nothing is fetched from the web");
            let after = format!("{out_after}/artifacts-16-images.png");
            shot(&out_after, "artifacts-16-images");
            let shift = vertical_shift(&format!("{out_after}/artifacts-16a-images-loading.png"), &after);
            check(report().at_end && shift.as_ref().is_ok_and(|s| s.abs() <= 1), &format!("images landing in their kept space move nothing: at end {}, shift {shift:?}", report().at_end));
            true
        })),
    ]);
    stages
}

// ---- a chat full of artifacts

/// Every type so far, the one ending on a clickable card last.
const ALL_TYPES: &[&str] = &["countdown", "decision", "question", "plan", "document", "research", "code_change", "data", "audio", "video", "file", "release", "directory", "workflow_run", "html_form"];

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
            headless::press("k");
            true
        })),
        ("K at the top of the screen", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            // (K reaching cards off screen is the artifact-keys check's.)
            let shown = crate::artifacts_view::on_screen(app);
            check(shown.contains(&selected(window)) && selected(window) != "form-stale-all", &format!("K again selects the card above, on screen: {:?} of {shown:?}", selected(window)));
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("selection off screen", Box::new(|app, window, elapsed| {
            // A card the list dropped lapses once the cards still drawn
            // have reported a few times since.
            let gone = !crate::artifacts_view::on_screen(app).contains(&app.artifact_cursor.borrow());
            if (!gone || elapsed < Duration::from_millis(600)) && elapsed < Duration::from_secs(5) {
                return false;
            }
            let before = (opened().len(), window.get_overlay().to_string());
            headless::press("o");
            let after = (opened().len(), window.get_overlay().to_string());
            check(gone && crate::artifacts_view::selected(app).is_none() && before == after, &format!("a selected card scrolled off screen is not acted on: off {gone}, {before:?} then {after:?}"));
            true
        })),
        // The app lets go of a card that scrolled away on its next clock
        // tick (every 250 ms).
        ("selection let go", Box::new(|app, window, elapsed| {
            if !selected(window).is_empty() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(selected(window).is_empty(), &format!("and shows no selection: {:?}", selected(window)));
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

// ---- resolved-decision receipts

/// The Host's prompt for a resolved decision, as
/// `artifacts.format_delivery_prompt` writes it.
fn resolved_prompt(decision: &str, artifact: &str, question: &str, outcome: &str) -> String {
    format!("[Clarp decision resolved]\nDecision ID: {decision}\nArtifact ID: {artifact}\nQuestion: {question}\nContext: Two reviews are in.\nReference: \nPayload: {{\"ticket\": \"OPS-12\"}}\n{outcome}")
}

/// A resolved decision shows as a receipt in the user's column: the
/// question, the outcome as a chip, the agent; no protocol line. J/K reach
/// it, O goes to the decision card it answers. Light and dark shots.
fn receipt_stages(out: &str) -> Vec<Stage> {
    let (out, out2, out3) = (out.to_owned(), out.to_owned(), out.to_owned());
    const SESSION: &str = "art-receipt";
    // Its cards' ids are its own (`-r`): the question chat before it has a
    // q-click too, and the keyboard's card there must not be one here.
    let receipts = [
        ("dec-approved-r", "Upgrade to Postgres 17?", "The user chose: accepted. Approval applies only to the described action. Revalidate it before acting.", "Approved", "success", "DECISION"),
        ("dec-declined-r", "Delete staging data?", "The user chose: rejected. Do not perform the protected action.", "Declined", "danger", "DECISION"),
        ("q-picked-r", "Which database?", "The user answered this clarification: {\"option_id\": \"lite\", \"label\": \"SQLite\"}. Continue using this answer. This does not grant approval for unrelated protected actions.", "SQLite", "answer", "QUESTION"),
        ("dec-discard-r", "Archive the 40 merged branches?", "The user discarded this request. This is not an answer or approval. Do not guess permission or repeat the unchanged request. Continue independent work only.", "Discarded", "muted", "DECISION"),
        ("dec-expired-r", "Book it before Friday?", "The request expired without an answer or approval. Do not infer a choice or perform the protected action. Continue independent work only.", "Expired", "muted", "DECISION"),
    ];
    let mut turns: Vec<Value> = receipts
        .iter()
        .enumerate()
        .map(|(i, (artifact, question, outcome, ..))| json!({"id": format!("receipt-{i}"), "role": "user", "origin": "automation", "text": resolved_prompt(&format!("d-{artifact}"), artifact, question, outcome)}))
        .collect();
    turns.push(json!({"id": "spoken", "role": "assistant", "text": "<speak>Noted <break time=\"350ms\"/> <vox>um</vox> I will keep the staging data.</speak>"}));
    vec![
        ("receipt load", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let loaded = control("/__control/artifact-chat", &json!({"session": SESSION, "persona": "Rachel", "types": ["decision", "question"], "suffix": "-r", "turns": turns}));
            check(loaded.is_ok(), &format!("the Host takes a chat with resolved decisions {}", loaded.err().unwrap_or_default()));
            true
        })),
        ("receipt open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find(SESSION).is_none() {
                return false;
            }
            app.engine.borrow_mut().select(SESSION);
            crate::pump();
            true
        })),
        ("receipt rows", Box::new(move |app, window, elapsed| {
            let shown: Vec<crate::MessageRow> = rows(window).into_iter().filter(|r| !r.receipt.key.is_empty()).collect();
            if (shown.len() < receipts.len() || !shown.iter().all(|r| r.receipt.linked) || elapsed < Duration::from_millis(400)) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(shown.len() == receipts.len(), &format!("each resolved decision is a receipt: {}", shown.len()));
            for ((artifact, question, _, outcome, tone, kind), row) in receipts.iter().zip(&shown) {
                let r = &row.receipt;
                check(row.author == "user" && row.blocks.row_count() == 0, &format!("{artifact}: in the user's column, no prompt text ({} blocks)", row.blocks.row_count()));
                check(r.question == *question && r.outcome == *outcome && r.tone == *tone && !r.mark.is_empty(), &format!("{artifact}: {:?} {:?} {:?} {:?}", r.question, r.outcome, r.tone, r.mark));
                check(r.artifact_id == *artifact && r.linked && r.kind == *kind && r.agent == "Rachel", &format!("{artifact}: answers its card, names the agent: {:?} {} {:?} {:?}", r.artifact_id, r.linked, r.kind, r.agent));
            }
            // What the rows draw: a prompt's text only through a block, a
            // reply's from its written form.
            let drawn: Vec<String> = app.engine.borrow().conversation(SESSION).map(|c| {
                c.rows().iter().filter(|m| rows(window).iter().any(|r| r.id == m.id.as_str() && r.blocks.row_count() > 0)).map(|m| if m.role == "user" { m.text.clone() } else { m.display_text.clone() }).collect()
            }).unwrap_or_default();
            let leaked: Vec<&String> = drawn.iter().filter(|t| ["Clarp decision resolved", "Decision ID", "Artifact ID", "Payload:", "<speak", "<break", "<vox", "um</vox>"].iter().any(|p| t.contains(p))).collect();
            check(leaked.is_empty(), &format!("no protocol line or voice tag reaches the chat: {leaked:?}"));
            check(drawn.iter().any(|t| t == "Noted, I will keep the staging data."), &format!("a spoken reply reads as written: {:?}", drawn.last()));
            check(app.active_messages().is_some(), "the chat is open");
            app.focus_transcript();
            true
        })),
        ("receipt end", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::End);
            true
        })),
        ("receipt dark shot", Box::new(move |app, _, elapsed| {
            let shown = crate::artifacts_view::on_screen(app);
            if !(elapsed >= Duration::from_millis(700) && shown.iter().any(|id| id == "receipt:receipt-4")) && elapsed < Duration::from_secs(4) {
                return false;
            }
            check(shown.iter().any(|id| id == "receipt:receipt-4"), &format!("the receipts report where they are, as cards do: {shown:?}"));
            shot(&out, "artifacts-20-receipts-dark");
            headless::press("k");
            true
        })),
        ("receipt selected", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let selected = crate::artifacts_view::selected(app).unwrap_or_default();
            check(selected == "receipt:receipt-4", &format!("K from the end is the last receipt: {selected:?}"));
            let hints = crate::artifacts_view::selected_hints(app).unwrap_or_default();
            check(hints == [("O".to_owned(), "Show decision".to_owned())], &format!("its key: {hints:?}"));
            shot(&out2, "artifacts-20b-receipt-selected");
            headless::press("o");
            true
        })),
        ("receipt opens its decision", Box::new(move |app, window, elapsed| {
            let selected = crate::artifacts_view::selected(app).unwrap_or_default();
            if selected != "dec-expired-r" && elapsed < Duration::from_secs(4) {
                return false;
            }
            check(selected == "dec-expired-r", &format!("O goes to the decision card it answers, in view: {selected:?}"));
            crate::view::apply_theme(window, "paper", None);
            headless::press(slint::platform::Key::End);
            true
        })),
        ("receipt light shot", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            shot(&out3, "artifacts-20c-receipts-light");
            crate::view::apply_theme(window, "terminal", None);
            true
        })),
    ]
}

/// `--check receipts`: the receipt stages alone.
pub(super) fn receipts_check(out: String) {
    run_stages(receipt_stages(&out));
}

pub(super) fn artifacts_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(countdown_stages(&out));
    stages.extend(html_form_stages(&out));
    stages.extend(decision_stages(&out));
    stages.extend(question_stages(&out));
    stages.extend(receipt_stages(&out));
    stages.extend(plan_stages(&out));
    stages.extend(document_stages(&out));
    stages.extend(research_stages(&out));
    stages.extend(code_change_stages(&out));
    stages.extend(data_stages(&out));
    stages.extend(audio_stages(&out));
    stages.extend(video_stages(&out));
    stages.extend(file_stages(&out));
    stages.extend(release_stages(&out));
    stages.extend(directory_stages(&out));
    stages.extend(workflow_stages(&out));
    stages.extend(image_stages(&out));
    stages.extend(scroll_stages(&out));
    run_stages(stages);
}

// ---- form events

/// An authenticated request to the fake Host, as the app makes them.
fn host_call(method: &str, path: &str, body: &Value) -> Result<(u16, Value), String> {
    use std::io::{Read, Write};
    let base = std::env::var("CLARP_BASE_URL").map_err(|e| e.to_string())?;
    let authority = base.trim_start_matches("http://").trim_end_matches('/').to_owned();
    let body = if body.is_null() { String::new() } else { body.to_string() };
    let mut stream = std::net::TcpStream::connect(&authority).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).map_err(|e| e.to_string())?;
    let token = std::env::var("CLARP_TOKEN").unwrap_or_default();
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {authority}\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply).map_err(|e| e.to_string())?;
    let status = reply.split(' ').nth(1).and_then(|s| s.parse().ok()).ok_or_else(|| format!("no status: {reply:?}"))?;
    let json = reply.split_once("\r\n\r\n").and_then(|(_, b)| serde_json::from_str(b).ok()).unwrap_or(Value::Null);
    Ok((status, json))
}

/// The rows the fake Host's journal holds for `artifact`, in its order.
fn host_events(artifact: &str) -> Vec<Value> {
    host_call("GET", &format!("/artifacts/{artifact}/events"), &Value::Null)
        .ok()
        .and_then(|(_, body)| body["events"].as_array().cloned())
        .unwrap_or_default()
}

/// The event ids of every batch the app sent for `artifact`, in order.
fn sent_batches(artifact: &str) -> Vec<Vec<String>> {
    posts(&format!("/artifacts/{artifact}/events"))
        .iter()
        .map(|post| post["body"]["events"].as_array().into_iter().flatten().filter_map(|e| e["event_id"].as_str().map(str::to_owned)).collect())
        .collect()
}

/// The app's event journals (`$XDG_DATA_HOME/clarp/form-events/*`).
fn journals() -> Vec<std::path::PathBuf> {
    let root = std::env::var_os("XDG_DATA_HOME").map(std::path::PathBuf::from).unwrap_or_default().join("clarp").join("form-events");
    std::fs::read_dir(root).into_iter().flatten().flatten().map(|e| e.path().join("events-outbox.jsonl")).filter(|p| p.exists()).collect()
}

fn journal_lines() -> usize {
    journals().iter().map(|p| std::fs::read_to_string(p).unwrap_or_default().lines().count()).sum()
}

/// The page's `window.clarpForm.log(event)`: its request to the loopback page.
fn log_event(url: &str, request_id: &str, event: &Value) -> Result<(u16, Value), String> {
    let (status, body) = fetch("POST", &format!("{url}/log"), &json!({"request_id": request_id, "event": event}).to_string(), None)?;
    Ok((status, serde_json::from_str(&body).unwrap_or(Value::Null)))
}

/// The first run: the form logs while the Host loses a receipt and then
/// refuses, and the app quits with the events still queued.
fn form_events_first_stages() -> Vec<Stage> {
    let form_url: Rc<RefCell<String>> = Rc::default();
    let (url2, url3) = (form_url.clone(), form_url);
    let logged: Rc<RefCell<Vec<String>>> = Rc::default();
    let (logged2, logged3) = (logged.clone(), logged.clone());
    let mut stages = load_chat("art-events", &["html_form"]);
    stages.extend::<Vec<Stage>>(vec![
        ("events form card", Box::new(move |_, window, elapsed| {
            if !placed(window, &["form-trip"], elapsed) {
                return false;
            }
            check(control("/__control/form-events", &json!({"lose_receipts": 1})).is_ok(), "the Host will lose the first receipt");
            window.global::<ArtifactBridge>().invoke_open("form-trip".into());
            true
        })),
        ("events form opens", Box::new(move |_, _, elapsed| {
            let Some(url) = opened().into_iter().find(|u| u.contains("/form/")) else {
                if elapsed > Duration::from_secs(3) {
                    check(false, &format!("the form opens: {:?}", opened()));
                    return true;
                }
                return false;
            };
            let page = fetch("GET", &url, "", None).map(|(_, body)| body).unwrap_or_default();
            check(page.contains("capabilities") && page.contains("eventLog: true"), "the bridge offers the event log on a Host that has it");
            for n in 0..3 {
                let id = uuid::Uuid::new_v4().to_string();
                let reply = log_event(&url, &id, &json!({"type": "number_shown", "number": n, "elapsed_ms": 1000 * n}));
                let queued = reply.as_ref().is_ok_and(|(status, body)| *status == 200 && *body == json!({"event_id": id, "queued": true, "synced": false}));
                check(queued, &format!("log({n}) resolves once the event is queued, not synced: {reply:?}"));
                logged.borrow_mut().push(id);
            }
            let again = log_event(&url, &logged.borrow()[0], &json!({"type": "number_shown", "number": 0, "elapsed_ms": 0}));
            check(again.as_ref().is_ok_and(|(status, _)| *status == 200), &format!("the same log again is the same event: {again:?}"));
            let conflict = log_event(&url, &logged.borrow()[0], &json!({"type": "other"}));
            check(conflict.as_ref().is_ok_and(|(status, body)| *status == 400 && body["error"].as_str().is_some_and(|e| e.contains("already"))), &format!("its id with another event is refused: {conflict:?}"));
            let array = fetch("POST", &format!("{url}/log"), &json!({"request_id": uuid::Uuid::new_v4().to_string(), "event": [1, 2]}).to_string(), None);
            check(array.as_ref().is_ok_and(|(status, _)| *status == 400), &format!("an event must be an object: {array:?}"));
            let big = log_event(&url, &uuid::Uuid::new_v4().to_string(), &json!({"t": "x".repeat(20_000)}));
            check(big.as_ref().is_ok_and(|(status, body)| *status == 400 && body["error"].as_str().is_some_and(|e| e.contains("16 KiB"))), &format!("an event over 16 KiB is refused visibly: {big:?}"));
            check(journal_lines() == 3, &format!("the three events are on disk: {} lines in {:?}", journal_lines(), journals()));
            *url2.borrow_mut() = url;
            true
        })),
        ("receipt lost", Box::new(move |_, _, elapsed| {
            let batches = sent_batches("form-trip");
            if batches.is_empty() || host_events("form-trip").len() < 3 {
                if elapsed > Duration::from_secs(5) {
                    check(false, "the app sends the queued events without a Send");
                    return true;
                }
                return false;
            }
            check(batches[0] == *logged2.borrow(), &format!("one batch carries the three events: {batches:?}"));
            check(host_events("form-trip").len() == 3, "the Host stored them before its receipt was lost");
            let failing = control("/__control/fail", &json!({"path": "/artifacts/form-trip/events", "status": 503, "count": 1000}));
            check(failing.is_ok(), "the Host goes away");
            let id = uuid::Uuid::new_v4().to_string();
            let reply = log_event(&url3.borrow(), &id, &json!({"type": "number_shown", "number": 3, "elapsed_ms": 3000}));
            check(reply.as_ref().is_ok_and(|(status, _)| *status == 200), &format!("logging carries on while the Host is away: {reply:?}"));
            logged2.borrow_mut().push(id);
            true
        })),
        ("events retried", Box::new(move |_, _, elapsed| {
            let batches = sent_batches("form-trip");
            if batches.len() < 2 {
                if elapsed > Duration::from_secs(8) {
                    check(false, &format!("the app retries after the lost receipt: {batches:?}"));
                    return true;
                }
                return false;
            }
            check(batches[1] == *logged3.borrow(), &format!("the retry sends the same ids, and the new event: {batches:?}"));
            check(journal_lines() == 4, &format!("all four stay queued: {}", journal_lines()));
            true
        })),
    ]);
    stages
}

/// The second run: the restarted app sends what the first left queued, the
/// Host has each event once, and a followed draft array is imported.
fn form_events_second_stages() -> Vec<Stage> {
    let mut stages: Vec<Stage> = vec![
        ("Host back", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/fail", &json!({"path": "/artifacts/form-trip/events", "status": 503, "count": 0})).is_ok(), "the Host is back");
            true
        })),
        ("queued events delivered", Box::new(|_, _, elapsed| {
            let rows = host_events("form-trip");
            let drained = journal_lines() == 0;
            if (rows.len() < 4 || !drained) && elapsed < Duration::from_secs(12) {
                return false;
            }
            let first_run: Vec<String> = sent_batches("form-trip").into_iter().flatten().fold(Vec::new(), |mut ids, id| {
                if !ids.contains(&id) {
                    ids.push(id);
                }
                ids
            });
            let ids: Vec<String> = rows.iter().filter_map(|r| r["event_id"].as_str().map(str::to_owned)).collect();
            check(rows.len() == 4 && ids == first_run, &format!("after the restart the Host has each logged event exactly once: {ids:?} (sent {first_run:?})"));
            let seqs: Vec<i64> = rows.iter().filter_map(|r| r["client_seq"].as_i64()).collect();
            check(seqs == [1, 2, 3, 4], &format!("in the order they were logged: {seqs:?}"));
            let numbers: Vec<i64> = rows.iter().filter_map(|r| r["event"]["number"].as_i64()).collect();
            check(numbers == [0, 1, 2, 3] && rows.iter().all(|r| r["version"] == 3 && r["client_at"].as_i64().is_some_and(|t| t > 1_700_000_000_000)), &format!("with their events, the form's version and client times: {rows:?}"));
            check(drained, &format!("and the app's journal is compacted: {} lines", journal_lines()));
            let followed = host_call("POST", "/artifacts/form-trip/events-config", &json!({"draft_key": "events"}));
            check(followed.as_ref().is_ok_and(|(status, _)| *status == 200), &format!("the agent follows the form's draft events: {followed:?}"));
            true
        })),
    ];
    stages.extend(load_chat("art-events", &["html_form"]));
    let form_url: Rc<RefCell<String>> = Rc::default();
    let url2 = form_url.clone();
    // The first run's pages are in the same list; this run's come after.
    let earlier = Rc::new(Cell::new(0));
    let earlier2 = earlier.clone();
    stages.extend::<Vec<Stage>>(vec![
        ("events form again", Box::new(move |_, window, elapsed| {
            if !placed(window, &["form-trip"], elapsed) {
                return false;
            }
            earlier.set(opened().len());
            window.global::<ArtifactBridge>().invoke_open("form-trip".into());
            true
        })),
        ("draft events", Box::new(move |_, _, elapsed| {
            let Some(url) = opened().into_iter().skip(earlier2.get()).find(|u| u.contains("/form/")) else {
                if elapsed > Duration::from_secs(3) {
                    check(false, &format!("the form opens again: {:?}", opened()));
                    return true;
                }
                return false;
            };
            let draft = json!({"run": 2, "events": [{"type": "tap", "i": 0}, {"type": "tap", "i": 1}]});
            let saved = fetch("POST", &format!("{url}/draft"), &draft.to_string(), None);
            check(saved.as_ref().is_ok_and(|(status, _)| *status == 200), &format!("the page saves a draft with an event array: {saved:?}"));
            *form_url.borrow_mut() = url;
            true
        })),
        ("draft events delivered", Box::new(move |_, _, elapsed| {
            let rows = host_events("form-trip");
            if rows.len() < 6 && elapsed < Duration::from_secs(8) {
                return false;
            }
            let taps: Vec<Value> = rows.iter().skip(4).map(|r| r["event"].clone()).collect();
            check(taps == [json!({"type": "tap", "i": 0}), json!({"type": "tap", "i": 1})], &format!("the followed array's entries reach the Host: {taps:?}"));
            let draft = json!({"run": 2, "events": [{"type": "tap", "i": 0}, {"type": "tap", "i": 1}, {"type": "tap", "i": 2}]});
            let saved = fetch("POST", &format!("{}/draft", url2.borrow()), &draft.to_string(), None);
            check(saved.as_ref().is_ok_and(|(status, _)| *status == 200), "the array grows by one");
            true
        })),
        ("draft delta delivered", Box::new(|_, _, elapsed| {
            let rows = host_events("form-trip");
            if (rows.len() < 7 || journal_lines() > 0) && elapsed < Duration::from_secs(8) {
                return false;
            }
            // A little longer, so a second copy would have arrived.
            if elapsed < Duration::from_secs(2) {
                return false;
            }
            let rows = host_events("form-trip");
            let ids: std::collections::HashSet<&str> = rows.iter().filter_map(|r| r["event_id"].as_str()).collect();
            check(rows.len() == 7 && ids.len() == 7 && rows[6]["event"] == json!({"type": "tap", "i": 2}), &format!("only the new entry is sent: {} rows {:?}", rows.len(), rows.last()));
            true
        })),
    ]);
    stages
}

/// `--check form-events` (run twice by check.sh, as `first` and `second`).
pub(super) fn form_events_check(_out: String) {
    match std::env::var("CLARP_CHECK_PASS").as_deref() {
        Ok("first") => run_stages(form_events_first_stages()),
        Ok("second") => run_stages(form_events_second_stages()),
        pass => {
            check(false, &format!("form-events runs as two passes, not {pass:?}"));
            super::finish();
        }
    }
}

/// `--check form-log`: on whatever Host CLARP_BASE_URL names (a real one
/// included), opens the form `CLARP_FORM_ARTIFACT`, logs
/// `CLARP_FORM_LOG_COUNT` (default 3) events through its page as
/// `window.clarpForm.log` does, and waits until the Host acknowledged them.
/// It writes nothing else to the Host.
pub(super) fn form_log_check(_out: String) {
    let artifact = std::env::var("CLARP_FORM_ARTIFACT").unwrap_or_default();
    let count: usize = std::env::var("CLARP_FORM_LOG_COUNT").ok().and_then(|v| v.parse().ok()).unwrap_or(3);
    let (artifact2, artifact3) = (artifact.clone(), artifact.clone());
    let before = Rc::new(Cell::new(0));
    let before2 = before.clone();
    run_stages(vec![
        ("Host keeps form events", Box::new(|app, _, elapsed| {
            let ready = app.engine.borrow().connected() && app.engine.borrow().form_events_supported();
            if !ready {
                if elapsed > Duration::from_secs(10) {
                    check(false, &format!("the Host keeps form events ({})", app.engine.borrow().connection_state()));
                    return true;
                }
                return false;
            }
            app.engine.borrow_mut().load_updates();
            true
        })),
        ("form listed", Box::new(move |app, window, elapsed| {
            if fixture(app, &artifact).is_null() {
                if elapsed > Duration::from_secs(10) {
                    check(false, &format!("{artifact} is among the Host's latest artifacts"));
                    return true;
                }
                return false;
            }
            before.set(opened().len());
            crate::artifacts_view::open(app, window, &artifact);
            true
        })),
        ("events logged", Box::new(move |_, _, elapsed| {
            let Some(url) = opened().into_iter().skip(before2.get()).find(|u| u.contains("/form/")) else {
                if elapsed > Duration::from_secs(3) {
                    check(false, &format!("{artifact2} opens as a page"));
                    return true;
                }
                return false;
            };
            let page = fetch("GET", &url, "", None).map(|(_, body)| body).unwrap_or_default();
            check(page.contains("capabilities") && page.contains("eventLog: true"), "the page offers the event log");
            for n in 0..count {
                let id = uuid::Uuid::new_v4().to_string();
                let reply = log_event(&url, &id, &json!({"type": "desktop_event_log_probe", "n": n, "client": "clarp-slint"}));
                check(reply.as_ref().is_ok_and(|(status, body)| *status == 200 && body["queued"] == true), &format!("log {id} queued: {reply:?}"));
            }
            true
        })),
        ("events synced", Box::new(move |_, _, elapsed| {
            if journal_lines() > 0 && elapsed < Duration::from_secs(12) {
                return false;
            }
            check(journal_lines() == 0 && !journals().is_empty(), &format!("the Host acknowledged every event logged for {artifact3}"));
            true
        })),
    ]);
}

// ---- opening an HTML artifact in the browser

/// The fake browser's launches (`CLARP_TEST_BROWSER_LOG`): its pid, then
/// its arguments, once it has fetched the page as a browser would.
fn browser_launches() -> Vec<(String, Vec<String>)> {
    let log = std::env::var("CLARP_TEST_BROWSER_LOG").unwrap_or_default();
    std::fs::read_to_string(&log).unwrap_or_default().lines().filter_map(|line| {
        let mut words = line.split_whitespace().map(str::to_owned);
        Some((words.next()?, words.collect()))
    }).collect()
}

/// The response the fake browser launched as `pid` got for its page.
fn browser_page(pid: &str) -> String {
    let log = std::env::var("CLARP_TEST_BROWSER_LOG").unwrap_or_default();
    std::fs::read_to_string(format!("{log}.page{pid}")).unwrap_or_default()
}

/// Whether the window is shown, as the compositor would be told.
fn shown(window: &crate::AppWindow) -> (bool, bool) {
    (window.window().is_visible(), window.window().is_minimized())
}

/// What a page and its URL must not carry: the Host's credential or address.
fn leaks(text: &str) -> Vec<&'static str> {
    let token = std::env::var("CLARP_TOKEN").unwrap_or_default();
    let host = std::env::var("CLARP_BASE_URL").unwrap_or_default();
    let mut found = Vec::new();
    if !token.is_empty() && text.contains(&token) { found.push("the token"); }
    if !host.is_empty() && text.contains(host.trim_end_matches('/')) { found.push("the Host's address"); }
    if text.to_ascii_lowercase().contains("set-cookie") { found.push("a cookie"); }
    found
}

/// Checks one launch: a new window of the default browser, with its own
/// options kept, on a loopback page that names no credential.
fn check_launch(what: &str, args: &[String], page: &str, content: &str) {
    let url = args.last().cloned().unwrap_or_default();
    check(args.iter().any(|a| a == "--new-window") && args.iter().any(|a| a == "--ozone-platform=wayland"),
        &format!("the {what} opens in a new window of the default browser, with its own options: {args:?}"));
    check(url.starts_with("http://127.0.0.1:") && url.contains("/form/"), &format!("from a loopback page: {url}"));
    check(leaks(&url).is_empty(), &format!("its address names no credential: {:?}", leaks(&url)));
    check(page.starts_with("HTTP/1.1 200") && page.contains(content), &format!("the browser got the {what}: {:?}", page.lines().next()));
    check(page.to_ascii_lowercase().contains("referrer-policy: no-referrer"), "which sends no referrer to the origins it reaches");
    check(leaks(page).is_empty(), &format!("and carries no credential: {:?}", leaks(page)));
}

/// `--check artifact-open`: HTML artifacts open for real (check.sh gives
/// the app a fake default browser), by a click and by the keyboard, and
/// Clarp stays: the window is still shown, the browser was not left as a
/// zombie, and nothing it got names the Host or its token.
pub(super) fn artifact_open_check(out: String) {
    let before: Rc<Cell<(bool, bool)>> = Rc::default();
    let (before2, before3) = (before.clone(), before.clone());
    let big = big_report_stages(&out);
    let mut stages = load_chat("art-open", &["html_open"]);
    stages.extend::<Vec<Stage>>(vec![
        ("cards", Box::new(move |_, window, elapsed| {
            if !placed(window, &["open-report", "open-form"], elapsed) {
                return false;
            }
            before.set(shown(window));
            // As a click on the card does.
            window.global::<ArtifactBridge>().invoke_open("open-report".into());
            true
        })),
        ("report opens", Box::new(move |app, window, elapsed| {
            let launches = browser_launches();
            if launches.is_empty() {
                if elapsed > Duration::from_secs(8) {
                    check(false, "a click on the report opens it in the browser");
                    return true;
                }
                return false;
            }
            let (pid, args) = &launches[0];
            check_launch("report", args, &browser_page(pid), "<h1>Rates</h1>");
            check(window.get_overlay().is_empty(), &format!("and not in Clarp's viewer: overlay {:?}", window.get_overlay()));
            check(shown(window) == before2.get() && !shown(window).1, &format!("Clarp's window stays shown: {:?} before {:?}", shown(window), before2.get()));
            // The keyboard leaves the card the click selected.
            crate::artifacts_view::leave(app);
            app.focus_transcript();
            true
        })),
        ("keyboard on the chat", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press("k");
            true
        })),
        ("form selected", Box::new(|_, window, elapsed| {
            if selected(window) != "open-form" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K selects the form: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            headless::press("o");
            true
        })),
        ("form opens", Box::new(move |_, window, elapsed| {
            let launches = browser_launches();
            if launches.len() < 2 {
                if elapsed > Duration::from_secs(8) {
                    check(false, &format!("O opens the form in the browser: {launches:?}"));
                    return true;
                }
                return false;
            }
            let (pid, args) = &launches[1];
            let page = browser_page(pid);
            check_launch("form", args, &page, "name=\"currency\"");
            check(page.contains("window.clarpForm"), "with its answer bridge");
            check(shown(window) == before3.get() && !shown(window).1, &format!("Clarp's window stays shown: {:?}", shown(window)));
            true
        })),
        ("openers reaped", Box::new(move |_, _, elapsed| {
            // A reaped child is gone from /proc; a zombie stays there.
            let lingering: Vec<String> = browser_launches().into_iter().map(|(pid, _)| pid).filter(|pid| std::path::Path::new(&format!("/proc/{pid}")).exists()).collect();
            if !lingering.is_empty() && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(lingering.is_empty(), &format!("the browsers it started were waited for, not left as zombies: {lingering:?}"));
            shot(&out, "artifact-open");
            true
        })),
    ]);
    stages.extend(big);
    run_stages(stages);
}

/// What the fake `xdg-open` was handed (`CLARP_TEST_OPENER_LOG`).
fn opener_calls() -> Vec<String> {
    let log = std::env::var("CLARP_TEST_OPENER_LOG").unwrap_or_default();
    std::fs::read_to_string(&log).unwrap_or_default().lines().map(str::to_owned).collect()
}

fn rss_kb() -> u64 {
    std::fs::read_to_string("/proc/self/status").unwrap_or_default().lines()
        .find_map(|l| l.strip_prefix("VmRSS:")).and_then(|v| v.split_whitespace().next()?.parse().ok()).unwrap_or(0)
}

/// Waits for the report viewer to close, then checks Clarp is still shown
/// with its chat.
fn back_to_chat(what: &'static str) -> Stage {
    (what, Box::new(move |_, window, elapsed| {
        if !window.get_overlay().is_empty() && elapsed < Duration::from_secs(2) {
            return false;
        }
        check(window.get_overlay().is_empty() && !rows(window).is_empty() && shown(window) == (true, false),
            &format!("{what}: Escape returns to the chat, the window still shown: overlay {:?} {:?}", window.get_overlay(), shown(window)));
        true
    }))
}

/// A report of the shape that was said to close Clarp: 1.16 MB, twelve
/// data: pictures, a data: font, an inline script, plain links. Opened by
/// every route; Clarp must stay, and its links must open in the browser.
fn big_report_stages(_out: &str) -> Vec<Stage> {
    let rss = Rc::new(Cell::new(0u64));
    let (rss2, launches_before) = (rss.clone(), Rc::new(Cell::new(0usize)));
    let launches_before2 = launches_before.clone();
    let mut stages = load_chat("art-big", &["html_big"]);
    stages.extend::<Vec<Stage>>(vec![
        ("big cards", Box::new(move |_, window, elapsed| {
            if !placed(window, &["big-report", "big-net"], elapsed) {
                return false;
            }
            rss.set(rss_kb());
            let started = std::time::Instant::now();
            window.global::<ArtifactBridge>().invoke_open("big-report".into());
            let took = started.elapsed().as_millis();
            check(window.get_overlay() == "report" && window.get_report_blocks().row_count() > 0,
                &format!("a click on the 1.16 MB report opens it in Clarp's viewer: overlay {:?}, {} blocks", window.get_overlay(), window.get_report_blocks().row_count()));
            check(took < 1_500, &format!("big report shown in {took} ms on the GUI thread (a debug build)"));
            check(shown(window) == (true, false), &format!("the window stays shown: {:?}", shown(window)));
            check(window.get_report_links() >= 4, &format!("its web links are listed: {}", window.get_report_links()));
            // A fragment link has nowhere to go outside the page.
            crate::open_link("#historier");
            crate::updates_view::open_report_link(0);
            true
        })),
        ("a link in it", Box::new(move |_, window, elapsed| {
            let calls = opener_calls();
            if calls.is_empty() && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(calls == ["https://www.digdir.no/media/2291/download"], &format!("its first link opens in the system browser, the fragment link nowhere: {calls:?}"));
            check(leaks(&calls.join(" ")).is_empty(), "with no credential");
            check(window.get_overlay() == "report", "and the report stays open");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        back_to_chat("after a click"),
        ("from Updates", Box::new(|_, window, _| {
            window.invoke_open_report("big-report".into());
            check(window.get_overlay() == "report" && window.get_report_blocks().row_count() > 0, &format!("Updates opens the big report: overlay {:?}", window.get_overlay()));
            headless::press(slint::platform::Key::Escape);
            true
        })),
        back_to_chat("after Updates"),
        ("keyboard", Box::new(|app, _, _| {
            crate::artifacts_view::leave(app);
            app.focus_transcript();
            true
        })),
        ("keyboard on the chat", Box::new(|_, _, elapsed| {
            if !report().transcript_focused && elapsed < Duration::from_secs(2) {
                return false;
            }
            headless::press("k");
            headless::press("k");
            true
        })),
        ("O on the report", Box::new(|_, window, elapsed| {
            if selected(window) != "big-report" {
                if elapsed > Duration::from_secs(2) {
                    check(false, &format!("K K selects the big report: {:?}", selected(window)));
                    return true;
                }
                return false;
            }
            // O, not Enter: Enter belongs to the composer.
            headless::press("o");
            true
        })),
        ("report by O", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "report" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_overlay() == "report", &format!("O opens the big report: overlay {:?}", window.get_overlay()));
            headless::press(slint::platform::Key::Escape);
            true
        })),
        back_to_chat("after O"),
        ("connected copy", Box::new(move |_, window, _| {
            launches_before.set(browser_launches().len());
            window.global::<ArtifactBridge>().invoke_open("big-net".into());
            true
        })),
        ("connected copy opens", Box::new(move |_, window, elapsed| {
            let launches = browser_launches();
            if launches.len() <= launches_before2.get() {
                if elapsed > Duration::from_secs(8) {
                    check(false, "the big report with a connection opens in the browser");
                    return true;
                }
                return false;
            }
            let (pid, args) = launches.last().cloned().unwrap_or_default();
            let page = browser_page(&pid);
            check(args.iter().any(|a| a == "--new-window") && page.starts_with("HTTP/1.1 200") && page.len() > 1_158_099 && page.contains("data:image/webp"),
                &format!("the big report with a connection is served whole to a new browser window: {} bytes", page.len()));
            check(leaks(&page).is_empty(), "with no credential");
            check(shown(window) == (true, false), &format!("the window stays shown: {:?}", shown(window)));
            true
        })),
        ("pointer click", Box::new(move |app, window, _| {
            app.focus_transcript();
            let (launches, opener) = (browser_launches().len(), window.get_overlay());
            let mut clicked = None;
            for y in (300..=700).rev().step_by(12) {
                super::click_at(window, 560.0, y as f32);
                if browser_launches().len() > launches || window.get_overlay() != opener {
                    clicked = Some(y);
                    break;
                }
            }
            check(clicked.is_some(), &format!("a real click on the latest big card opens it (at y {clicked:?})"));
            let grew = rss_kb().saturating_sub(rss2.get());
            check(shown(window) == (true, false), &format!("Clarp is alive and shown after every route; memory grew {grew} kB"));
            true
        })),
    ]);
    stages
}
