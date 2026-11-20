//! The permanent voice timeline: normalize at admission and compare on the corrected clock.
use crate::python;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};

pub const MAX_BATCH: usize = 500;
const SIX_HOURS: i64 = 6 * 60 * 60 * 1000;

fn text(value: &Value) -> String {
    value
        .as_str()
        .map_or_else(|| python::repr(value), str::to_owned)
}

fn or_blank(value: &Value) -> String {
    if python::truthy(value) {
        text(value)
    } else {
        String::new()
    }
}

fn cut(value: &Value, max: usize, keep_empty: bool) -> Value {
    if value.is_null() {
        return Value::Null;
    }
    let s = text(value);
    if !keep_empty && s.is_empty() {
        Value::Null
    } else {
        json!(python::first(&s, max))
    }
}

fn float(value: &Value) -> Option<f64> {
    let number = match value {
        Value::Number(n) => n.as_f64()?,
        Value::String(s) => {
            let mut out = String::new();
            let mut digits = false;
            let mut chars = python::strip(s).chars().peekable();
            while let Some(c) = chars.next() {
                if c == '_' {
                    if !digits
                        || chars
                            .peek()
                            .and_then(|c| python::decimal_digit(*c))
                            .is_none()
                    {
                        return None;
                    }
                } else if let Some(d) = python::decimal_digit(c) {
                    out.push(char::from_digit(d, 10)?);
                } else {
                    out.push(c);
                }
                digits = python::decimal_digit(c).is_some();
            }
            out.parse::<f64>().ok()?
        }
        _ => return None,
    };
    number.is_finite().then_some(number)
}

fn int(value: &Value) -> Option<i64> {
    float(value).map(|n| n as i64)
}

fn detail(value: &Value) -> Value {
    if !value.is_object() {
        return json!({});
    }
    let encoded = python::compact_ascii(value);
    if encoded.len() > 8000 {
        json!({"truncated":true,"head":&encoded[..7936]})
    } else {
        value.clone()
    }
}

pub struct Batch {
    pub rows: Vec<Value>,
    pub offset: Option<i64>,
}

pub fn batch(body: &Value, received: i64) -> Result<Batch, (u16, &'static str)> {
    if !body.is_object() {
        return Err((400, "bad json"));
    }
    let events = body["events"]
        .as_array()
        .ok_or((400, "events must be a list"))?;
    if events.len() > MAX_BATCH {
        return Err((413, "batch too large"));
    }
    let offset = int(&body["sent_at"])
        .and_then(|sent| received.checked_sub(sent))
        .filter(|v| v.unsigned_abs() <= SIX_HOURS as u64);
    let source = or_blank(&body["source"]);
    let source = python::strip(&source).to_lowercase();
    let source = match source.as_str() {
        "ios" | "android" | "server" | "pwa" => source.as_str(),
        _ => "other",
    };
    let client = or_blank(&body["client_id"]);
    let session = or_blank(&body["session"]);
    let default = json!(python::strip(&session));
    let mut rows = Vec::new();
    for item in events {
        let name = or_blank(&item["event"]);
        let name = python::strip(&name);
        if name.is_empty() {
            continue;
        }
        let client_ts = int(&item["ts"]);
        let ts = client_ts
            .zip(offset)
            .and_then(|(t, o)| t.checked_add(o))
            .unwrap_or(received);
        rows.push(json!({"ts":ts,"client_ts":client_ts,"mono_ms":int(&item["mono_ms"]),
            "received_at":received,"clock_offset_ms":offset,"source":source,
            "client_id":cut(&json!(python::strip(&client)),128,false),
            "session":cut(if python::truthy(&item["session"]){&item["session"]}else{&default},128,false),
            "utterance_id":cut(&item["utterance_id"],128,false),"trace_id":cut(&item["trace_id"],128,false),
            "event":python::first(name,64),"duration_ms":float(&item["duration_ms"]),"level_db":float(&item["level_db"]),
            "peak_db":float(&item["peak_db"]),"text":cut(&item["text"],4000,true),"detail":detail(&item["detail"])}));
    }
    Ok(Batch { rows, offset })
}

pub fn server_row(name: &str, fields: Value, received: i64) -> Value {
    // Server records have their own event time and may carry a raw, uncorrected client clock.
    json!({"ts":fields["ts"].as_i64().unwrap_or(received),"client_ts":int(&fields["client_ts"]),
        "mono_ms":null,"received_at":received,"clock_offset_ms":null,"source":"server","client_id":null,
        "session":cut(&fields["session"],128,false),"utterance_id":cut(&fields["utterance_id"],128,false),
        "trace_id":cut(&fields["trace_id"],128,false),"event":python::first(python::strip(name),64),
        "duration_ms":float(&fields["duration_ms"]),"level_db":float(&fields["level_db"]),"peak_db":float(&fields["peak_db"]),
        "text":cut(&fields["text"],4000,true),"detail":detail(&fields["detail"])})
}

#[derive(Default)]
pub struct Query {
    pub session: String,
    pub since: Option<i64>,
    pub until: Option<i64>,
    pub utterance: String,
    pub trace: String,
    pub event: String,
    pub limit: Option<i64>,
}

impl Query {
    pub fn read(params: &[(String, String)]) -> Self {
        let first = |name: &str| {
            params
                .iter()
                .find(|(k, _)| k == name)
                .map_or("", |(_, v)| python::strip(v))
        };
        let num = |name| int(&json!(first(name)));
        Self {
            session: first("session").into(),
            since: num("since"),
            until: num("until"),
            utterance: first("utterance_id").into(),
            trace: first("trace_id").into(),
            event: first("event").into(),
            limit: num("limit"),
        }
    }

    fn limit(&self, default: i64, maximum: i64) -> usize {
        self.limit
            .filter(|n| *n != 0)
            .unwrap_or(default)
            .clamp(1, maximum) as usize
    }

    /// Input is newest first on (ts,event_id). Apply filters before taking the limit.
    pub fn rows<'a>(
        &self,
        newest: impl Iterator<Item = &'a Value>,
        grouped: bool,
    ) -> Vec<&'a Value> {
        let limit = if grouped { 5000 } else { self.limit(500, 5000) };
        let mut rows: Vec<_> = newest
            .filter(|r| {
                let ts = r["ts"].as_i64().unwrap_or(0);
                self.since.is_none_or(|n| ts >= n)
                    && self.until.is_none_or(|n| ts <= n)
                    && (grouped
                        || [
                            ("session", &self.session),
                            ("utterance_id", &self.utterance),
                            ("trace_id", &self.trace),
                            ("event", &self.event),
                        ]
                        .into_iter()
                        .all(|(field, q)| q.is_empty() || r[field].as_str() == Some(q.as_str())))
            })
            .take(limit)
            .collect();
        rows.reverse();
        rows
    }

    pub fn utterances(&self, chronological: &[&Value]) -> Vec<Value> {
        let mut traces = HashMap::new();
        let mut groups = Vec::<(&str, Vec<&Value>)>::new();
        let mut positions = BTreeMap::new();
        for &row in chronological {
            if let Some(uid) = row["utterance_id"].as_str().filter(|s| !s.is_empty()) {
                let i = *positions.entry(uid).or_insert_with(|| {
                    groups.push((uid, Vec::new()));
                    groups.len() - 1
                });
                groups[i].1.push(row);
                if let Some(t) = row["trace_id"].as_str() {
                    traces.entry(t).or_insert(i);
                }
            }
        }
        for &row in chronological {
            if row["utterance_id"].is_null()
                && let Some(i) = row["trace_id"].as_str().and_then(|t| traces.get(t))
            {
                groups[*i].1.push(row);
            }
        }
        let mut out = Vec::new();
        for (uid, mut events) in groups {
            if !self.session.is_empty() && !events.iter().any(|r| r["session"] == self.session) {
                continue;
            }
            events.sort_by_key(|e| (e["ts"].as_i64(), e["event_id"].as_u64()));
            let first = |name: &str| events.iter().copied().find(|e| e["event"] == name);
            let last = |name: &str| events.iter().copied().rfind(|e| e["event"] == name);
            let start = first("speech_start");
            let end = last("speech_end");
            let transcript = first("transcript");
            let send = first("send");
            let play = first("play_start");
            let upload = last("upload_end");
            let level = end
                .filter(|e| !e["level_db"].is_null())
                .or_else(|| last("level"));
            let field = |r: Option<&Value>, key: &str| r.map_or(Value::Null, |e| e[key].clone());
            let delta =
                |a: Option<&Value>, b: Option<&Value>| {
                    json!(a.zip(b).and_then(|(a, b)| Some(
                        b["ts"].as_i64()? as i128 - a["ts"].as_i64()? as i128
                    )))
                };
            let duration = |a, b| {
                let d = field(b, "duration_ms");
                if d.is_null() { delta(a, b) } else { d }
            };
            let mut reasons = Vec::new();
            for e in events.iter().filter(|e| e["event"] == "corrupt") {
                let d = &e["detail"];
                let r = if python::truthy(&d["reasons"]) {
                    &d["reasons"]
                } else {
                    &d["reason"]
                };
                if let Some(items) = r.as_array() {
                    reasons.extend(items.iter().map(text));
                } else if python::truthy(r) {
                    reasons.push(text(r));
                }
            }
            out.push(json!({"utterance_id":uid,"session":events.iter().find_map(|e|e["session"].as_str()),
                "trace_id":events.iter().find_map(|e|e["trace_id"].as_str()),"started_at":start.unwrap_or(events[0])["ts"],
                "ended_at":field(end,"ts"),"speech_ms":duration(start,end),"silence_before_ms":first("silence").map_or_else(||start.map_or(Value::Null,|e|e["detail"]["silence_ms"].clone()),|e|e["duration_ms"].clone()),
                "level_db":field(level,"level_db"),"peak_db":field(level,"peak_db"),
                "upload_ms":duration(first("upload_start"),upload),"stt_ms":field(transcript,"duration_ms"),
                "transcript":field(transcript,"text"),"speech_to_text_ms":delta(end,transcript),
                "text_to_send_ms":delta(transcript,send),"send_to_play_ms":delta(send,play),
                "speech_to_play_ms":delta(end,play),"play_ms":delta(play,last("play_end")),
                "barge_in":first("barge_in").is_some(),"corrupt":reasons,"events":events.len()}));
        }
        out.sort_by_key(|r| std::cmp::Reverse(r["started_at"].as_i64()));
        out.truncate(self.limit(100, 2000));
        out
    }
}
