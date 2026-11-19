#!/usr/bin/env python3
"""Each site changes a model row, selection of a worker, or the outcome of removal."""
import runner

RUNS = {
    "catalog": ["test", "--test", "transcription_inventory_vectors"],
    "host": ["test", "--test", "host", "--", "installed_transcription_models_are_selected_and_removed_by_their_own_records"],
    "worker": ["test", "-p", "clarp-speech", "--lib", "an_installed_variant_loads_before_its_first_clip"],
}
PLANTS = []


def changes(file, run, pairs):
    for old, new in pairs:
        PLANTS.append((file, run, f"{old} becomes {new}", old, new))


changes("clarp-kernel/src/heard/inventory.rs", "catalog", [
    ('"Base English"', '"Base"'), ('"Small English"', '"Small"'), ('"Small Multilingual"', '"Small"'),
    ('"Medium"', '"Medium English"'), ('"Large v3 Turbo"', '"Large Turbo"'),
    ('"whisper.cpp Base English"', '"Base English"'), ('"whisper.cpp Small English"', '"Small English"'),
    ('1_530_000_000', '1_500_000_000'), ('1_620_000_000', '1_600_000_000'),
    ('"very-heavy"', '"heavy"'), ('installed.contains(&id)', '!installed.contains(&id)'),
    ('platform == "linux"', 'platform == "macos"'), ('id == "faster-whisper:small.en"', 'id == "faster-whisper:small"'),
    ('"download_bytes": bytes', '"download_bytes": bytes / 1000'), ('"languages": [language]', '"languages": language'),
    ('"platforms": [platform]', '"platforms": platform'), ('"provider": provider', '"provider": model'),
    ('"weight": weight', '"weight": name'), ('"model": model', '"model": name'),
    ('"id": id', '"id": model'), ('"name": name', '"name": model'), ('"status": status', '"status": "available"'),
    ('!name.starts_with', 'name.starts_with'), ('"config.json", "model.bin", "tokenizer.json"', '"model.bin", "tokenizer.json"'),
    ('"config.json", "model.bin", "tokenizer.json"', '"config.json", "tokenizer.json"'),
    ('"config.json", "model.bin", "tokenizer.json"', '"config.json", "model.bin"'),
])
changes("clarp-host/src/heard.rs", "host", [
    ('catalog(&registered)', 'catalog(&[])'), ('!registered.contains(&id)', 'registered.contains(&id)'),
    ('self.own_id().as_deref() != Some(model)', 'self.own_id().as_deref() == Some(model)'),
    ('post(inventory::remove_model)', 'get(inventory::remove_model)'),
])
changes("clarp-host/src/heard/inventory.rs", "host", [
    ('records.iter().position(|r| r["id"] == id)', 'records.iter().position(|r| r["id"] != id)'),
    ('!record["_managed_root"].is_null()', 'record["_managed_root"].is_null()'),
    ('installed_path(record).is_none()', 'installed_path(record).is_some()'),
    ('r["id"] == model', 'r["id"] != model'), ('kept("transcription-models", "records".into())', 'kept("transcription-models", "unknown".into())'),
    ('records.remove(index);', 'let _ = index;'),
    ('own_id().as_deref().unwrap_or("") == id', 'own_id().as_deref().unwrap_or("") != id'),
    ('text.trim().to_owned()', 'text.to_owned()'), ('"model_id": id', '"model_id": ""'),
    ('*loaded == id','*loaded != id'),
    ('cannot remove the configured server default;', 'cannot remove default;'),
])
changes("clarp-host/src/heard/managed.rs", "host", [
    ('id.replace(\':\', "--")', 'id.replace(\':\', "-")'),
    ('!resolved.starts_with(&resolved_root)', 'resolved == resolved_root'),
    ('refusing to remove model outside managed caches:', 'model outside managed caches:'),
    ('refusing to remove anything but the managed folder for', 'invalid model folder for'),
    ('target != absolute_root.join(&name)', 'target == absolute_root.join(&name)'),
    ('resolved != target', 'resolved == target'),
    ('std::fs::remove_dir_all(self.path())', 'Ok(())'),
])
changes("clarp-host/src/heard/variants.rs", "host", [
    ('loaded == id','loaded != id'), ('slot.as_ref()', 'None::<&(String, Arc<Whisper>)>'),
    ('self.inference.clone().lock_owned()', 'Arc::new(AsyncMutex::new(())).lock_owned()'),
    ('*slot = Some((selected, worker.clone()))', '*slot = None'),
])
changes("clarp-speech/src/whisper.rs", "worker", [
    ('could not load transcription model {id}: {error}', 'could not load transcription model {id}'),
    ('*locked(&loaded.state) = State::Up(child)', 'drop(child)'),
])
if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
