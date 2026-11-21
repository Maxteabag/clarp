#!/usr/bin/env python3
"""Each site changes a model row, selection of a worker, or the outcome of removal."""
import runner

RUNS = {
    "port": ["test", "-p", "clarp-host", "--lib", "unreadable_inventory_requires_a_working_store"],
    "door": ["test", "--test", "host", "a_change_the_python_host_has_a_route_for_is_not_built_here"],
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
    ('let Some(stored) = record.get("_managed_root")', 'let Some(stored) = record.get("_private_unused")'),
    ('installed_path(record).is_none()', 'installed_path(record).is_some()'),
    ('r["id"] == model', 'r["id"] != model'), ('judged("transcription-models", "records".into())', 'judged("transcription-models", "unknown".into())'),
    ('records.remove(index);', 'let _ = index;'),
    ('text.trim().to_owned()', 'text.to_owned()'), ('"model_id": id', '"model_id": ""'),
])
changes("clarp-host/src/heard/install.rs", "host", [
    ('own_id().as_deref().unwrap_or("") == id', 'own_id().as_deref().unwrap_or("") != id'),
    ('loaded == id','loaded != id'),
    ('cannot remove the configured server default;', 'cannot remove default;'),
])
changes("clarp-host/src/heard/managed.rs", "host", [
    ('let name = id.replace(\':\', "--");', 'let name = id.replace(\':\', "-");'),
    ('if kind.is_symlink()', 'if !kind.is_symlink()'),
    ('its folder is a link or outside the managed models folder','invalid managed path'),
    ('Some(Path::new(s)) == expected.as_deref()', 'Some(Path::new(s)) != expected.as_deref()'),
    ('reason=stored_path','reason=bad_folder'),
    ('if self.directory','if !self.directory'),
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
changes("clarp-host/src/heard/inventory.rs", "host", [
    ('if record["_remove_pending"] != true', 'if record["_remove_pending"] == true'),
    ('if Some(id) == active', 'if Some(id) != active'),
    ('fields.remove("_remove_pending");', 'fields.remove("_unused");'),
    ('if !pending {', 'if pending {'),
    ('records[index]["_remove_pending"] = json!(true)', 'records[index]["_remove_pending"] = json!(false)'),
    ('if record["_remove_pending"] == true', 'if record["_remove_pending"] == false'),
    ('if unreadable {','if !unreadable {'),
])
# Its unreadable list is set aside by the one loader now (`kept.rs`): the words and the copy's name are there.
changes("clarp-host/src/kept.rs", "host", [
    ('"clarp-host: a record of the kind {} could not be read and is set aside {}: its desk starts without it"', '"corrupt record {}{}"'),
    ('pub const SET_ASIDE: &str = ".unreadable";', 'pub const SET_ASIDE: &str = ".backup";'),
])
changes("clarp-host/src/heard/managed.rs", "host", [
    ('Ok(dir) => dir,\n                        Err(e) if e.kind() == std::io::ErrorKind::NotFound', 'Ok(dir) => dir,\n                        Err(e) if e.kind() != std::io::ErrorKind::NotFound'),
    ('Ok(meta) => meta.file_type(),\n            Err(e) if e.kind() == std::io::ErrorKind::NotFound', 'Ok(meta) => meta.file_type(),\n            Err(e) if e.kind() != std::io::ErrorKind::NotFound'),
])
# The 501 for install has been replaced by a working 202 route. Its rules live in transcription-installer.
# The copy and a refused write are the loader's (`kept::judge`, through `FleetHandle::judged`).
changes("clarp-host/src/kept.rs", "port", [
    ('(table.to_string(), aside.clone(), Some(stored)),', '(table.to_string(), aside.clone(), Some(Vec::new())),'),
    ('store.put_together(&moved)?;', 'let _ = store.put_together(&moved);'),
])
changes("clarp-host/src/heard/inventory.rs", "port", [
    ('.await\n            .map_err(|e| e.0)?;', '.await.unwrap_or(crate::kept::Found::Unfit);'),
])
if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
