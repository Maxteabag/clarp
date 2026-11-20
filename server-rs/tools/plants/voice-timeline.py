#!/usr/bin/env python3
"""Wrong voice timeline rules: batch normalization, ordering, association and latency."""
import runner

RUNS={"kernel":["test","--test","voice_timeline"]}
K="clarp-kernel/src/voice_timeline.rs"
PLANTS=[]


def rules(pairs):
    for old,new in pairs:
        PLANTS.append((K,"kernel",f"{old} becomes {new}",old,new))


rules([
    ("MAX_BATCH: usize = 500", "MAX_BATCH: usize = 499"),
    ("MAX_BATCH: usize = 500", "MAX_BATCH: usize = 501"),
    ('"events must be a list"', '"missing events"'), ('"batch too large"', '"too large"'),
    ('"bad json"','"bad body"'), ("SIX_HOURS: i64 = 6 * 60 * 60 * 1000","SIX_HOURS: i64 = 6 * 60 * 60 * 1000 - 1"),
    ("SIX_HOURS: i64 = 6 * 60 * 60 * 1000","SIX_HOURS: i64 = 6 * 60 * 60 * 1000 + 1"),
    ("received.checked_sub(sent)","sent.checked_sub(received)"),
    (".zip(offset)",".zip(Some(0))"),
    ('int(&item["ts"])','int(&item["client_ts"])'), ('int(&item["mono_ms"])','int(&item["ts"])'),
    ('to_lowercase()','to_uppercase()'), ('"pwa" => source.as_str()','"tablet" => source.as_str()'),
    ('"android"','"invalid-android"'), ('"ios"','"invalid-ios"'), ('"server" | "pwa"','"invalid-server"|"pwa"'),
    ('"source":source','"source":"other"'), ('"received_at":received,"clock_offset_ms":offset','"received_at":ts,"clock_offset_ms":offset'),
    ('"clock_offset_ms":offset','"clock_offset_ms":received'), ('"client_ts":client_ts','"client_ts":ts'),
    ('python::first(name,64)','python::first(name,63)'),('python::first(name,64)','python::first(name,65)'),
    ('"text":cut(&item["text"],4000,true)','"text":cut(&item["text"],3999,true)'),
    ('"text":cut(&item["text"],4000,true)','"text":cut(&item["text"],4001,true)'),
    ('"text":cut(&item["text"],4000,true)','"text":cut(&item["text"],4000,false)'),
    ('"duration_ms":float(&item["duration_ms"])','"duration_ms":float(&item["level_db"])'),
    ('"peak_db":float(&item["peak_db"])','"peak_db":float(&item["level_db"])'),
    ('"level_db":float(&item["level_db"])','"level_db":float(&item["peak_db"])'),
    ('encoded.len() > 8000','encoded.len()>=8000'), ('encoded.len() > 8000','encoded.len()>8001'),
    ('&encoded[..7936]','&encoded[..7935]'), ('"truncated":true','"truncated":false'),
    ('python::truthy(&item["session"])','!python::truthy(&item["session"])'),
    ('rows.reverse();','let _ = &rows;'),
    ('traces.entry(t).or_insert(i)','traces.insert(t,i)'),
    ('|| r[field].as_str() == Some(q.as_str())','||r[field].as_str()!=Some(q.as_str())'),
    ('ts >= n','ts>n'), ('ts <= n','ts<n'), ('self.limit(500, 5000)','self.limit(1,5000)'),
    ('self.limit(100, 2000)','self.limit(1,2000)'),('k == name','k!=name'),
    ('b["ts"].as_i64()? as i128 - a["ts"].as_i64()? as i128','a["ts"].as_i64()? as i128 - b["ts"].as_i64()? as i128'),
    ('first("speech_start")','last("speech_start")'),('last("speech_end")','first("speech_end")'),
    ('let transcript = first("transcript")','let transcript=last("transcript")'),('first("play_start")','last("play_start")'),
    ('last("play_end")','first("play_end")'), ('"speech_to_play_ms":delta(end,play)','"speech_to_play_ms":delta(start,play)'),
    ('"play_ms":delta(play','"play_ms":delta(send'),('"events":events.len()','"events":1'), ('first("barge_in").is_some()','first("barge_in").is_none()'),
    ('std::cmp::Reverse(r["started_at"].as_i64())','r["started_at"].as_i64()'),
])
RUNS.update({
    "read":["test","--test","host","voice_timeline_rows_survive_restart"],
    "produce":["test","--test","host","server_voice_timeline_joins_transcription"],
    "store":["test","-p","clarp-host","--lib","failed_voice_timeline_writes_are_private"],
})
def change(file, run, pairs):
    for old,new in pairs:
        PLANTS.append((file,run,f"{old} becomes {new}",old,new))

change("clarp-host/src/fleet.rs","store",[
    ('shelf("voice-event-sequence")','shelf("voice-events")'),
    ('table.clone(), format!("{last:020}")','table.clone(), format!("{last}")'),
    ('row["event_id"] = serde_json::json!(last)','row["event_id"] = serde_json::json!(last+1)'),
    ('shelf("voice-events")','shelf("other-events")'),
    ('self.works.log.unkept("voice-events", &error.0)','self.works.log.unkept("events", &error.0)'),
])
change("clarp-host/src/voices.rs","read",[
    ('get(event_rows).post(voice_events)','get(utterance_rows).post(voice_events)'),
    ('"/voice-events/utterances", get(utterance_rows)','"/voice-events/utterances", get(event_rows)'),
    ('"stored":count','"stored":0'),
    ('q.utterances(&selected)','Vec::<Value>::new()'),
])
change("clarp-host/src/voice.rs","read",[
    ('"play-start" => Some("play_start")','"play-start" => None'),
    ('"play-ok" => Some("play_end")','"play-ok" => None'),
])
change("clarp-host/src/voice.rs","produce",[
    ('"play-fail" => Some("play_fail")','"play-fail" => None'),
])
change("clarp-host/src/heard.rs","produce",[
    ('"retry",','"retired-retry",'),
    ('"transcript",','"transcribed",'),
    ('"herald_consumed":answer["herald_consumed"]','"herald_consumed":true'),
    ('"reasons":["bad_size"]','"reasons":["bad_audio"]'),
    ('"client_ts":client_ts,"duration_ms":duration','"client_ts":null,"duration_ms":duration'),
    ('"bytes":length,"content_type":content_type,','"bytes":0,"content_type":content_type,'),
])
change("clarp-host/src/voice_timeline.rs","produce",[
    ('"level_db":metric["rms_db"]','"level_db":metric["peak_db"]'),
    ('"reasons":measured["reasons"]','"reasons":[]'),
    ('"dispatch":response.dispatch,"client_msg_id":client_msg_id,"queued"','"dispatch":"","client_msg_id":client_msg_id,"queued"'),
])
RUNS["metrics"]=["test","--test","host","voice_timeline_analyser_matches_the_synthetic_pcm_reference"]
change("../tools/voice-metrics.py","metrics",[
    ("duration < MIN_SPEECH_MS","duration <= MIN_SPEECH_MS"),
    ("> CLIP_RATIO_LIMIT",">= CLIP_RATIO_LIMIT"),
    ("< QUIET_PEAK_DB","<= QUIET_PEAK_DB"),
    ("> SILENT_RATIO_LIMIT",">= SILENT_RATIO_LIMIT"),
    ("> DC_OFFSET_LIMIT",">= DC_OFFSET_LIMIT"),
    ('return ["decode_error"]','return ["decode"]'),
    ('and silent_text:',':'),
    ('abs(float(metrics.get("dc_offset") or 0.0))','float(metrics.get("dc_offset") or 0.0)'),
])
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
