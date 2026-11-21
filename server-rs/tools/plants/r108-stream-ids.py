"""Wrong rules for keyed native imports and a Claude reply with several text blocks."""
import runner

R = 'clarp-backend/src/history/rollout.rs'
P = 'clarp-host/src/past.rs'
C = 'clarp-backend/src/claude.rs'
RUNS = {
    'codex': ['test', '--test', 'codex_history'],
    'seed': ['test', '-p', 'clarp-host', '--lib', 'native_provider_rows_keep_their_ids_when_unrelated_lines_move'],
    'live': ['test', '--test', 'host', 'claude_text_blocks_grow_one_item_and_one_durable_reply'],
}
PLANTS = [
    (R, 'codex', 'a modern item has no provider id',
     'say(&mut self.turns, role, &text, at, of("phase"), &provider_id);',
     'say(&mut self.turns, role, &text, at, of("phase"), "");'),
    (R, 'codex', 'a user item is keyed as an assistant',
     'let provider_id = if kind == "agent_message" {',
     'let provider_id = if matches!(kind.as_str(), "agent_message" | "user_message") {'),
    (R, 'codex', 'the legacy record keeps no id when the modern item comes',
     'if !provider_id.is_empty() && !python::truthy(&last["provider_id"]) {',
     'if false {'),
    (R, 'codex', 'a second copy replaces the provider id',
     'if !provider_id.is_empty() && !python::truthy(&last["provider_id"]) {',
     'if !provider_id.is_empty() {'),
    (P, 'seed', 'every imported row is named by its position',
     'let key = format!("{namespace}\\0provider\\0{provider}\\0{occurrence}");',
     'let key = format!("{namespace}\\0{number}");'),
    (P, 'seed', 'a repeated Codex item id names one row',
     '*occurrence += 1;',
     '*occurrence = 0;'),
    (P, 'seed', 'a raw user item is keyed like an assistant',
     '.filter(|id| !id.is_empty() && turn["role"] == "assistant");',
     '.filter(|id| !id.is_empty());'),
    (C, 'live', 'a new item is made for each text block',
     'let id = self.live_items.entry(row.clone()).or_insert(id).clone();',
     'let id = format!("{id}:{}", draft.blocks.last().map_or(0, |(at, _)| *at));'),
    (C, 'live', 'a live item has just the latest words',
     'let text = draft.text();\n            let row = self.reply_id(draft);',
     'let text = draft.blocks.last().map(|(_, text)| text.clone()).unwrap_or_default();\n            let row = self.reply_id(draft);'),
    (C, 'live', 'a root reply also emits items for its individual blocks',
     'self.shown.see(&event, None, out);',
     'self.shown.see(&event, Some(&|message| Some(message.to_string())), out);'),
    (C, 'live', 'the reply item points at no row',
     'row: Some(row.clone()),',
     'row: None,'),
    (C, 'live', 'the reply item is settled only when the entire turn ends',
     'out.push(Signal::Glimpse(Glimpse::Done { id: item }));',
     'let _ = item;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
