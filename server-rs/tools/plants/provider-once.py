#!/usr/bin/env python3
"""Faults in R175's ordered engine plans, boundary evidence, and receipts."""
import runner

RUNS = {
    "vectors": ["test", "-p", "clarp-kernel", "--test", "provider_once_vectors"],
    "host": ["test", "-p", "clarp-host", "--test", "host", "--", "r175_engine_order::"],
}
LANG = "clarp-kernel/src/languages.rs"
ORDER = "clarp-kernel/src/heard/engine_order.rs"
CALL = "clarp-kernel/src/call_transcription.rs"
UPLOAD = "clarp-host/src/heard.rs"
CALL_UPLOAD = "clarp-host/src/heard/call.rs"
PROFILE = "clarp-kernel/src/voice_profile.rs"
PLANTS = []


def rules(path, suite, changes):
    for description, old, new in changes:
        PLANTS.append((path, suite, description, old, new))


rules(LANG, "vectors", [
    ("the stored list retains exact repeats",
     ".filter(|part| seen.insert(*part))", ".filter(|part| { let _ = seen.insert(*part); true })"),
    ("the stored parser counts padding",
     "value.split(',').map(strip).filter", "value.split(',').filter"),
    ("empty engine fields are kept",
     ".filter(|part| !part.is_empty())", ".filter(|_| true)"),
    ("saving keeps repeated entries",
     'Some(engines(text).join(","))', 'Some(named_engines(text).collect::<Vec<_>>().join(","))'),
    ("saving sorts instead of retaining the first position",
     'Some(engines(text).join(","))',
     'Some({ let mut names = engines(text); names.sort(); names.join(",") })'),
    ("saving skips validation",
     "let known = named_engines(text).all(is_valid);",
     "let known = { let _ = is_valid; true };"),
])
rules(ORDER, "vectors", [
    ("the empty name does not identify the own engine",
     'matches!(name, "" | "server-default" | "local")', 'matches!(name, "server-default" | "local")'),
    ("server-default is its own separate engine",
     'matches!(name, "" | "server-default" | "local")', 'matches!(name, "" | "local")'),
    ("local is a separate engine",
     'matches!(name, "" | "server-default" | "local")', 'matches!(name, "" | "server-default")'),
    ("the own model ID is treated like an unrelated local model",
     "|| own_model == Some(name)", '|| own_model == Some("unused")'),
    ("engine identity keeps outer whitespace",
     "let name = strip(engine);", "let name = engine;"),
    ("knowledge of one model suppresses every other one of its provider before an attempt",
     ".partition(|engine| seen.insert(identity(engine, own_model)))",
     ".partition(|engine| seen.insert(hosted_provider(engine).unwrap_or_else(|| identity(engine, own_model))))"),
    ("the distinct list is rebuilt from its last occurrences",
     ".partition(|engine| seen.insert(identity(engine, own_model)))",
     ".collect::<Vec<_>>().into_iter().rev().partition(|engine| seen.insert(identity(engine, own_model)))"),
    ("unknown engines exhaust a fake hosted provider",
     ".find(|known| known.id == provider)", '.find(|known| known.id == provider || provider == "other")'),
    ("only the first provider is ever recognized",
     ".find(|known| known.id == provider)", '.find(|known| known.id == provider && known.id == "deepgram")'),
    ("a local attempt before the boundary does not count toward R152",
     "let Some(provider) = hosted_provider(engine) else {\n            return true;",
     "let Some(provider) = hosted_provider(engine) else {\n            return handed;"),
    ("a refusal before provider contact exhausts the provider",
     "if handed {\n            self.sent.insert(provider);",
     "if !handed {\n            self.sent.insert(provider);"),
    ("the failed list includes a hosted refusal before its boundary",
     "        handed\n    }", "        true\n    }"),
    ("provider contact is ignored by later attempts",
     "hosted_provider(engine).filter(|provider| self.sent.contains(provider))",
     "hosted_provider(engine).filter(|provider| !self.sent.contains(provider))"),
    ("every attempt gets the bounded selection wait",
     "Some((engine, !self.pending.is_empty()))",
     "Some((engine, true))"),
    ("the presence of an eligible fallback is ignored after a failure",
     "has_fallback: !self.pending.is_empty(),", "has_fallback: false,"),
    ("the filtered list keeps the same provider's later model",
     "skipped.push((*later, provider));\n                false",
     "skipped.push((*later, provider));\n                true"),
    ("an unnamed session logs an empty focus",
     'if focus.is_empty() { "-" } else { focus }', "focus"),
    ("the repeat is logged as though the provider already received it",
     "{engine} (listed before)", "{engine} (already sent)"),
    ("the provider-contact skip says the engine was merely listed before",
     "{engine} ({provider} already had this recording)", "{engine} ({provider} listed before)"),
])
rules(CALL, "vectors", [
    ("a call's documented provider skip reason changes",
     '"provider_already_sent"', '"provider_already_used"'),
    ("a pre-existing language skip is replaced by the provider reason",
     "self.skip.or_else(|| {\n            contacts",
     "self.skip.filter(|_| contacts.already_sent(&self.engine).is_none()).or_else(|| {\n            contacts"),
    ("skipped call steps count as runnable fallbacks",
     "|step| step.skip_reason(contacts).is_none()",
     "|step| step.skip_reason(contacts).is_some()"),
])
rules(UPLOAD, "host", [
    ("the upload excludes a provider after a refusal before contact",
     "let failure = order.failed(model, handed.is_some());",
     "let failure = order.failed(model, handed.is_none());"),
])
rules(CALL_UPLOAD, "host", [
    ("a call does not tell the reason for skipping a contacted provider",
     "report.skipped(&step.engine, reason);", "report.skipped(&step.engine, if reason.is_empty() { reason } else { \"skipped\" });"),
    ("a call ignores provider contact when it advances",
     "contacted.failed(&step.engine, handed.is_some());",
     "contacted.failed(&step.engine, handed.is_none());"),
])
rules(PROFILE, "host", [
    ("hearing counts the actual default model as a separate engine",
     ".chat_order(Some(&view.own_engine))", ".chat_order(None)"),
    ("hearing shows the repeated names instead of the planned list",
     ".chat_order(Some(&view.own_engine))\n        .0", ".chat_order(Some(&view.own_engine))\n        .1"),
])

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
