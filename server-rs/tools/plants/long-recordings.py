#!/usr/bin/env python3
"""Each change mismeasures a clip or sends its upload to the wrong engine."""
import runner

RUNS = {'kernel':['test','--test','long_recordings_vectors'],
        'host':['test','--test','host','--','long_recordings_are_routed_by_duration_on_upload']}
K='clarp-kernel/src/heard/duration.rs'
P='clarp-kernel/src/call_transcription.rs'
H='clarp-host/src/heard.rs'
C='clarp-host/src/heard/call.rs'
W='clarp-speech/src/whisper.rs'
PYWORKER='../tools/whisper-worker.py'
PLANTS=[]


def change(file, run, pairs):
    for old,new in pairs:
        PLANTS.append((file,run,f'{old} becomes {new}',old,new))


change(K,'kernel',[
    ('!= b"RIFF"', '== b"RIFF"'), ('!= b"WAVE"', '== b"WAVE"'),
    ('12..limit','8..limit'), ('b"fmt "', 'b"fmt0"'),
    ('1 => {}','3 => {}'), ('0xfffe','0xfffd'),
    ('data.get(24..40)?','data.get(22..38)?'),
    ('0, 56, 155, 113','0, 56, 155, 112'),
    ('word(data.get(2..)?)?', 'word(data.get(0..)?)?'),
    ('size(data.get(4..)?)?', 'size(data.get(8..)?)?'),
    ('word(data.get(14..)?)?', 'word(data.get(12..)?)?'),
    ('.div_ceil(8)', '/ 8'), ('channels * width','width'),
    ('frame == 0 || rate == 0','frame == 0 && rate == 0'),
    ('length as u32 / frame','length as u32'),
    ('f64::from(frames) / f64::from(rate)','f64::from(frames / rate)'),
    ('b"data"','b"DATA"'), ('format = Some((frame, rate))','format.get_or_insert((frame,rate))'),
    ('checked_add(length % 2)?', 'checked_add(0)?'),
    ('s >= threshold as f64','s > threshold as f64'), ('s >= threshold as f64','s >= threshold as f64 - 0.01'),
    ('model.trim()','model'),
])
change(P,'kernel',[
    ('Some("device" | "global")','Some("device")'),
    ('Some("device" | "global")','Some("global")'),
    ('Some("device" | "global")','Some("device" | "global" | "agent")'),
    ('Some("device" | "global")','Some("device" | "global" | "call_policy")'),
    ('self.order = vec![model.to_string()]','self.order.push(model.to_string())'),
    ('self.allows_long_model() && !model.is_empty()', 'self.allows_long_model()'),
])
change(H,'host',[
    ('if model.is_empty() {\n            return "";','if !model.is_empty() {\n            return "";'),
    ('Some(s) => Some(s)','Some(_) => None'),
    ('let long = if own.is_empty()', 'let long = if !own.is_empty()'),
    ('heard::plan(requested, own, cloud, long_model, false)','heard::plan(requested, own, cloud, "", false)'),
    ('settings.push((LONG_FORM_MODEL_KEY.to_string(), model.to_string()))','settings.push((LONG_FORM_MODEL_KEY.to_string(), String::new()))'),
    ('if !model.is_empty() && unknown(model)', 'if unknown(model)'),
    ('"long_form_model must be a string"', '"bad model"'),
    ('"long_form_threshold_sec must be between 1 and 3600"', '"invalid threshold"'),
    ('"long_form_threshold_sec must be an integer"', '"not an integer"'),
    ('"long_form_threshold_range": { "min": 1, "max": 3600 }','"long_form_threshold_range": { "min": 0, "max": 3601 }'),
])
change(C,'host',[
    ('if resolved.allows_long_model()', 'if !resolved.allows_long_model()'),
    ('resolved.use_long_model(long);', 'resolved.use_long_model("");'),
])
change(W,'host',[
    ('json!({"op":"probe","id":id,"path":path})','json!({"op":"transcribe","id":id,"path":path})'),
    ('v["seconds"].as_f64()', 'v["duration"].as_f64()'),
])
change(PYWORKER,'host',[
    ("if s.type == 'audio'", "if s.type == 'video'"),
    ('if container.duration:', 'if False:'),
    (' / av.time_base',' * av.time_base'),
    ('if stream.duration and stream.time_base:', 'if False:'),
    ('stream.duration * stream.time_base','stream.duration / stream.time_base'),
    ('sum(frame.samples for frame in container.decode(stream))', 'max(frame.samples for frame in container.decode(stream))'),
    ('samples / rate if rate > 0', 'samples if rate > 0'),
    ("elif op == 'probe':", "elif op == 'duration':"),
])
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
