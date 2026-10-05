//! HTML forms in the browser. The desktop has no web view, so an agent's
//! form is served from a loopback page with the iOS answer bridge
//! (`window.clarpForm`), its drafts and answers posted back to this page
//! instead of a WebKit message handler. Answers go to the Host from the
//! app, with its token; the page never sees the token or the Host.
//!
//! Each form gets a random path; requests naming another Host (DNS
//! rebinding) or coming from another origin are refused.
//!
//! On a Host that keeps form events, `window.clarpForm.log(event)` posts to
//! the page too: the event is in the form's journal on disk before the page
//! hears "queued", and the engine sends it on (`clarp_engine::FormEvents`).

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde_json::{Value, json};

/// The largest request body a page may post.
const MAX_BODY: usize = 1 << 20;

struct Form {
    artifact_id: String,
    version: Value,
    html: String,
    /// The latest draft the page saved, restored when it opens again.
    draft: Value,
    /// Where its events are queued; None on a Host without them.
    events: Option<Events>,
}

/// A form's event journal, and the Host its events belong to.
#[derive(Clone)]
pub struct Events {
    pub store: clarp_engine::FormEventStore,
    pub key: String,
    pub origin: String,
}

fn lock_events(store: &clarp_engine::FormEventStore) -> std::sync::MutexGuard<'_, clarp_engine::FormEvents> {
    store.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The engine sends what the page queued (it is on disk either way).
fn events_changed(key: String) {
    let handed = slint::invoke_from_event_loop(move || {
        if let Some(app) = crate::app() {
            app.engine.borrow_mut().form_events_changed(&key);
            crate::pump_now(&app);
        }
    });
    if let Err(error) = handed {
        eprintln!("clarp-slint: queued form events wait for the next start: {error}");
    }
}

fn show_status(artifact: String, status: String) {
    let handed = slint::invoke_from_event_loop(move || {
        if let Some(app) = crate::app() {
            app.engine.borrow_mut().set_artifact_status(&artifact, &status);
            crate::pump_now(&app);
        }
    });
    if let Err(error) = handed {
        eprintln!("clarp-slint: {error}");
    }
}

fn forms() -> &'static Mutex<HashMap<String, Form>> {
    static FORMS: OnceLock<Mutex<HashMap<String, Form>>> = OnceLock::new();
    FORMS.get_or_init(Mutex::default)
}

/// The loopback port, once the listener runs.
fn port() -> Result<u16, String> {
    static PORT: OnceLock<Result<u16, String>> = OnceLock::new();
    PORT.get_or_init(|| {
        let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("cannot serve forms: {e}"))?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        std::thread::Builder::new()
            .name("form-server".into())
            .spawn(move || {
                for stream in listener.incoming() {
                    match stream {
                        Ok(stream) => {
                            let spawned = std::thread::Builder::new().name("form-request".into()).spawn(move || handle(stream, port));
                            if let Err(error) = spawned {
                                eprintln!("clarp-slint: cannot answer a form request: {error}");
                            }
                        }
                        Err(error) => eprintln!("clarp-slint: form server: {error}"),
                    }
                }
            })
            .map_err(|e| format!("cannot serve forms: {e}"))?;
        Ok(port)
    })
    .clone()
}

/// The page for `artifact_id`'s form, kept for the window's life: opening
/// it again returns the same page with its draft.
pub fn serve(artifact_id: &str, version: Value, html: &str, events: Option<Events>) -> Result<String, String> {
    let port = port()?;
    let mut forms = forms().lock().map_err(|_| "the form list is poisoned".to_owned())?;
    let token = match forms.iter().find(|(_, f)| f.artifact_id == artifact_id).map(|(t, _)| t.clone()) {
        Some(token) => {
            let form = forms.get_mut(&token).expect("found above");
            form.version = version;
            form.html = html.to_owned();
            form.events = events;
            token
        }
        None => {
            let token = uuid::Uuid::new_v4().simple().to_string();
            forms.insert(token.clone(), Form { artifact_id: artifact_id.to_owned(), version, html: html.to_owned(), draft: json!({}), events });
            token
        }
    };
    Ok(format!("http://127.0.0.1:{port}/form/{token}"))
}

struct Request {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

fn read_request(stream: &TcpStream) -> Result<Request, String> {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).map_err(|e| e.to_string())?;
    let mut parts = line.split_whitespace();
    let (method, path) = (parts.next().unwrap_or_default().to_owned(), parts.next().unwrap_or_default().to_owned());
    let mut headers = HashMap::new();
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header).map_err(|e| e.to_string())? == 0 || header.trim().is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_owned());
        }
    }
    let length: usize = headers.get("content-length").and_then(|l| l.parse().ok()).unwrap_or(0);
    if length > MAX_BODY {
        return Err(format!("a {length}-byte body is too large"));
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body).map_err(|e| e.to_string())?;
    Ok(Request { method, path, headers, body })
}

fn respond(mut stream: &TcpStream, status: u16, content_type: &str, body: &str) {
    let reason = match status {
        200 => "OK",
        202 => "Accepted",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n",
        body.len()
    );
    if let Err(error) = stream.write_all(head.as_bytes()).and_then(|()| stream.write_all(body.as_bytes())) {
        eprintln!("clarp-slint: a form page went away: {error}");
    }
}

fn handle(stream: TcpStream, port: u16) {
    if let Err(error) = stream.set_read_timeout(Some(Duration::from_secs(10))) {
        eprintln!("clarp-slint: form request: {error}");
    }
    let request = match read_request(&stream) {
        Ok(request) => request,
        Err(error) => {
            eprintln!("clarp-slint: unreadable form request: {error}");
            return respond(&stream, 400, "text/plain", "unreadable request");
        }
    };
    let own = format!("127.0.0.1:{port}");
    let host_ok = request.headers.get("host").is_some_and(|h| *h == own);
    let origin_ok = request.headers.get("origin").is_none_or(|o| *o == format!("http://{own}"));
    if !host_ok || !origin_ok {
        return respond(&stream, 403, "text/plain", "forbidden");
    }
    let path = request.path.split('?').next().unwrap_or_default();
    let Some(rest) = path.strip_prefix("/form/") else { return respond(&stream, 404, "text/plain", "not found") };
    let (token, action) = rest.split_once('/').unwrap_or((rest, ""));
    let Ok(mut forms) = forms().lock() else { return respond(&stream, 500, "text/plain", "form list unavailable") };
    let Some(form) = forms.get_mut(token) else { return respond(&stream, 404, "text/plain", "not found") };
    match (request.method.as_str(), action) {
        ("GET", "") => {
            let page = page(&form.html, &form.draft, form.events.is_some());
            drop(forms);
            respond(&stream, 200, "text/html; charset=utf-8", &page);
        }
        ("POST", "log") => {
            let Some(events) = form.events.clone() else {
                drop(forms);
                return respond(&stream, 400, "application/json", &json!({"error": "This Host does not keep form events."}).to_string());
            };
            drop(forms);
            let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let (Some(event_id), Some(event)) = (body["request_id"].as_str(), body.get("event")) else {
                return respond(&stream, 400, "application/json", r#"{"error":"a log needs a request_id and an event"}"#);
            };
            let logged = lock_events(&events.store).log(&events.key, event, event_id, &events.origin);
            match logged {
                Ok(()) => {
                    events_changed(events.key);
                    respond(&stream, 200, "application/json", &json!({"event_id": event_id, "queued": true, "synced": false}).to_string());
                }
                Err(error) => {
                    eprintln!("clarp-slint: a form event was not queued: {error}");
                    respond(&stream, 400, "application/json", &json!({"error": error}).to_string());
                }
            }
        }
        ("POST", "draft" | "submit") => {
            let answers: Value = match serde_json::from_slice(&request.body) {
                Ok(value @ Value::Object(_)) => value,
                _ => return respond(&stream, 400, "application/json", r#"{"error":"answers must be a JSON object"}"#),
            };
            form.draft = answers.clone();
            if let Some(events) = form.events.clone() {
                // A draft array the Host follows is imported as events.
                match lock_events(&events.store).draft(&events.key, &answers, &events.origin) {
                    Ok(0) => {}
                    Ok(_) => events_changed(events.key),
                    Err(error) => {
                        eprintln!("clarp-slint: a form's draft events were not queued: {error}");
                        show_status(form.artifact_id.clone(), format!("Event log: {error}"));
                    }
                }
            }
            if action == "submit" {
                let (artifact, version) = (form.artifact_id.clone(), form.version.clone());
                drop(forms);
                let handed = slint::invoke_from_event_loop(move || {
                    if let Some(app) = crate::app() {
                        app.engine.borrow_mut().submit_html_form(&artifact, version, answers);
                        crate::pump_now(&app);
                    }
                });
                if let Err(error) = handed {
                    eprintln!("clarp-slint: dropped a form's answers: {error}");
                    return respond(&stream, 500, "application/json", r#"{"error":"the app is closing"}"#);
                }
                return respond(&stream, 202, "application/json", r#"{"ok":true,"status":"sending"}"#);
            }
            drop(forms);
            respond(&stream, 200, "application/json", r#"{"ok":true}"#);
        }
        _ => {
            drop(forms);
            respond(&stream, 404, "text/plain", "not found");
        }
    }
}

/// The form with iOS's policy (no network but this page, no frames, no
/// navigation) and its bridge, answers posted to this page.
fn page(html: &str, draft: &Value, event_log: bool) -> String {
    // Inside a script, "</" could close it early.
    let saved = draft.to_string().replace("</", "<\\/");
    let policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; connect-src 'self'; frame-src 'none'; form-action 'none'; base-uri 'none'";
    let bridge = format!(
        r#"(() => {{
  let saved = {saved}, custom = null;
  function collect() {{
    if (custom !== null) return custom;
    const result = {{}};
    for (const e of document.querySelectorAll('input[name],select[name],textarea[name]')) {{
      if (e.disabled || ['button','submit','file','password'].includes(e.type)) continue;
      if (e.type === 'radio') {{ if (e.checked) result[e.name] = e.value; continue; }}
      result[e.name] = e.type === 'checkbox' ? e.checked : (e.type === 'number' || e.type === 'range' ? e.valueAsNumber : e.value);
    }}
    return result;
  }}
  const say = (text) => {{ const s = document.getElementById('clarp-status'); if (s) s.textContent = text; }};
  const post = (type, answers) => fetch(location.pathname + '/' + type, {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(answers)}})
    .then((r) => {{ if (type === 'submit') say(r.ok ? 'Sent to Clarp. The chat shows whether the agent got them.' : 'Clarp did not take the answers.'); }})
    .catch(() => say('Clarp is not running.'));
  function uuid() {{
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'));
    return [hex.slice(0, 4), hex.slice(4, 6), hex.slice(6, 8), hex.slice(8, 10), hex.slice(10)].map((p) => p.join('')).join('-');
  }}
  let logging = 0;
  // Resolves once Clarp has the event on disk (queued, not yet synced).
  function log(event) {{
    if (!event || typeof event !== 'object' || Array.isArray(event)) return Promise.reject(new Error('Event must be an object.'));
    if (!{event_log}) return Promise.reject(new Error('This Host does not keep form events.'));
    if (logging >= 128) return Promise.reject(new Error('Too many pending log calls.'));
    let body;
    try {{ body = JSON.stringify({{request_id: uuid(), event}}); }} catch (error) {{ return Promise.reject(error); }}
    logging++;
    const promise = fetch(location.pathname + '/log', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body}})
      .then(async (r) => {{
        const result = await r.json().catch(() => ({{}}));
        if (r.ok && result.queued === true) return result;
        throw new Error(result.error || 'Event was not recorded.');
      }}, () => {{ throw new Error('Clarp is not running; the event was not recorded.'); }})
      .finally(() => {{ logging--; }});
    promise.catch(() => {{}});
    return promise;
  }}
  window.clarpForm = {{capabilities: {{eventLog: {event_log}}}, collect, getDraft: () => saved, log,
    setAnswers: (answers) => {{ custom = answers; saved = answers; post('draft', answers); }},
    submit: (answers) => {{ if (answers !== undefined) custom = answers; post('draft', collect()); post('submit', collect()); }}}};
  document.addEventListener('DOMContentLoaded', () => {{
    for (const e of document.querySelectorAll('[name]')) if (Object.hasOwn(saved, e.name)) {{
      if (e.type === 'checkbox') e.checked = !!saved[e.name]; else if (e.type === 'radio') e.checked = e.value === saved[e.name]; else e.value = saved[e.name];
    }}
    window.dispatchEvent(new CustomEvent('clarpformready', {{detail: saved}}));
  }});
  document.addEventListener('input', () => post('draft', collect()));
  document.addEventListener('change', () => post('draft', collect()));
  document.addEventListener('submit', (e) => {{ e.preventDefault(); window.clarpForm.submit(); }});
}})();"#
    );
    let bar = "<div id=\"clarp-bar\" style=\"position:fixed;left:0;right:0;bottom:0;padding:10px 16px;background:#1a1b26;color:#e7e1dc;font:14px sans-serif;display:flex;gap:12px;align-items:center\">\
<button type=\"button\" onclick=\"window.clarpForm.submit()\" style=\"font:inherit;padding:6px 14px\">Send answers</button>\
<span id=\"clarp-status\">Drafts are kept by Clarp until you send.</span></div><div style=\"height:56px\"></div>";
    format!(
        "<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
<meta http-equiv=\"Content-Security-Policy\" content=\"{policy}\"><script>{bridge}</script>{html}{bar}"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_draft_cannot_close_the_bridge_script() {
        let page = page("<form></form>", &json!({"note": "</script><script>alert(1)"}), false);
        assert!(!page.contains("</script><script>alert"), "{page}");
        assert!(page.contains("connect-src 'self'"));
    }

    #[test]
    fn the_bridge_offers_the_event_log_only_on_a_host_that_keeps_events() {
        assert!(page("", &json!({}), true).contains("capabilities: {eventLog: true}"));
        assert!(page("", &json!({}), false).contains("capabilities: {eventLog: false}"));
    }
}
