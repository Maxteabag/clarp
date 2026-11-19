#!/usr/bin/env python3
"""Wrong rules for the controller tutorial's spoken line and cache."""
import runner

K = 'clarp-kernel/src/controller_narration.rs'
H = 'clarp-host/src/voices.rs'
V = 'clarp-host/src/voice.rs'
RUNS = {
    'vectors': ['test', '--test', 'controller_narration_vectors'],
    'door': ['test', '--test', 'host', 'the_controller_speaks_a_short_line_and_keeps_only_its_primary_voice'],
    'store': ['test', '--lib', 'the_preview_store_serves_only_the_opened_regular_file_of_its_owner'],
}
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, why, old, new) for why, old, new in changes)


rules(K, 'vectors', [
    ('a different persona speaks', '"Rachel"', '"Other"'),
    ('the last text field wins', '.find(|(key, _)|', '.rfind(|(key, _)|'),
    ('query keys stay encoded', 'crate::media::unquoted(key) == "text"', '*key == "text"'),
    ('another query key is accepted', '== "text"', '== "line"'),
    ('a literal plus becomes a blank', 'crate::media::unquoted(raw)', 'crate::media::unquoted(&raw.replace(\'+\', " "))'),
    ('query text stays encoded', 'crate::media::unquoted(raw)', 'raw.to_owned()'),
    ('nonprinting characters remain', 'crate::python::printed(sign)', 'sign != char::MAX'),
    ('separating spaces disappear', '.join(" ")', '.join("")'),
    ('an empty normalized line is accepted', '!line.is_empty()', 'true'),
    ('the last character is lost', '<= 240', '< 240'),
    ('the first oversized line is accepted', '<= 240', '<= 241'),
    ('the budget counts bytes', 'line.chars().count()', 'line.len()'),
    ('another digest version', 'v1\\0', 'v2\\0'),
    ('no delimiter after the model', '{model}\\0{text}', '{model}{text}'),
    ('the digest ignores voice', '{voice}\\0{model}', '{model}'),
    ('the digest ignores model', '{model}\\0{text}', '{text}'),
    ('the digest ignores text', '{model}\\0{text}', '{model}'),
])
rules(H, 'door', [
    ('an invalid line has the wrong explanation', 'text required (at most 240 characters)', 'text required'),
    ('a missing voice is a synthesis failure', 'StatusCode::SERVICE_UNAVAILABLE,\n            "narration voice unavailable"', 'StatusCode::BAD_GATEWAY, "narration voice unavailable"'),
    ('the refusal omits narration', '"narration voice unavailable"', '"voice unavailable"'),
    ('a cache hit bypasses the key check', 'voice.filter(|_| app.mouth.has_key("cartesia", &owners))', 'voice'),
    ('ask for the wrong persona', 'settings.cartesia_voice_for(PERSONA)', 'settings.cartesia_voice_for("Nobody")'),
    ('use a different primary', 'preview_with(&owners, "cartesia", voice', 'preview_with(&owners, "deepgram", voice'),
    ('the key ignores the configured model', '&owner::cartesia_model(&owners)', '"default"'),
    ('the cache refusal has the wrong words', '"narration cache unavailable"', '"narration unavailable"'),
    ('a failed engine answers as missing credentials', 'StatusCode::BAD_GATEWAY, "narration unavailable"', 'StatusCode::SERVICE_UNAVAILABLE, "narration unavailable"'),
    ('failed synthesis has the wrong words', '"narration unavailable"', '"synthesis failed"'),
])
rules(V, 'door', [
    ('the narration cache has no prefix', '"narration-"', '""'),
    ('fallback enters the cache', 'return Ok(Preview::Temporary(Bytes::from(bytes)));\n                    }', 'if let Some(ref f) = file { std::fs::write(f, &bytes).unwrap(); } return Ok(Preview::Temporary(Bytes::from(bytes)));\n                    }'),
    ('a hit is synthesized again', 'return Ok(hit);', 'drop(hit);'),
    ('cache parent permits a linked directory', 'crate::files::open(&self.previews, true)', 'std::fs::File::open(&self.previews)'),
    ('a dangling cache link loses its refusal', 'if self.previews.is_symlink()', 'if false'),
    ('the store is created without owner execute permission', '.mode(0o700)\n                .create(&self.previews)', '.mode(0o600)\n                .create(&self.previews)'),
    ('a cached range ignores its start', 'file.read_at(&mut bytes, at)', 'file.read_at(&mut bytes, 0)'),

])
rules(V, 'store', [
    ('another owner is accepted', 'meta.uid() == owner', 'meta.uid() == owner || meta.is_file()'),
    ('a directory counts as a clip', 'meta.is_file() && meta.uid()', 'meta.uid()'),
    ('follow a cache file link', 'crate::files::open(path, false).ok()?', 'std::fs::File::open(path).ok()?'),
])
rules('clarp-host/src/edge.rs','door',[('the feature has another contract','("controller_narration", 8)','("controller_narration", 9)')])

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
