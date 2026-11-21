#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What may stop the Host from starting: the one loader (`kept.rs`: each row read alone, a bad one left out, counted,
said by kind and key and never by what it holds, an owner's write to its key the repair, the door's counts only while
there are any) and the fleet's start on it (`fleet.rs`: every kind through the loader, the names of agents it keeps
and does not run held, a deleted agent's goals retired at a later start, the store watched). Held by the loader's own
tests and the fleet's tests at the port (`fleet/tolerance_tests.rs`: stores written by their own crate before a
start, a store that does not answer).
"""
import runner

RUNS = {
    'kept': ['test', '-p', 'clarp-host', '--lib', 'kept::tests'],
    'fleet': ['test', '-p', 'clarp-host', '--lib', 'fleet::tolerance_tests'],
    'opening': ['test', '-p', 'clarp-host', '--lib', 'calls::opening::tests'],
    'registry': ['test', '-p', 'clarp-host', '--lib', 'kept_registry'],
    'host': ['test', '--test', 'host', '--', 'start_tolerance::'],
    'vocabulary': ['test', '-p', 'clarp-host', '--lib', 'vocabulary::store::tests'],
    'quota': ['test', '--test', 'host', '--', 'start_tolerance::startup_preserves_the_quota_key'],
}
E = 'clarp-host/src/edge.rs'
S = 'clarp-host/src/schedules.rs'
O = 'clarp-host/src/calls/opening.rs'
K = 'clarp-host/src/kept.rs'
F = 'clarp-host/src/fleet.rs'
V = 'clarp-host/src/vocabulary/store.rs'
L = 'clarp-host/src/library/events.rs'
M = 'clarp-host/src/model_fallbacks.rs'
P = 'clarp-host/src/places.rs'
OM = 'clarp-host/src/oracle/memory.rs'
Q = 'clarp-host/src/quota.rs'
T = 'clarp-host/src/turn_identity.rs'
# NOT PLANTED, each with its reason:
#   the client's chat judged as an object (`edge.rs`): the reader and the guard are the place's, planted there; its key
#     is a digest of who the client is, which a Host test cannot write before the client is.
#   the start line (`main.rs`): what it says is `LeftOut::line`, planted here; that it is said is the Host's test's.
#   the calls' recovery record by record and the hold by agent: held by the units `calls::opening::tests` and
#     `voice::held::tests` (planted here); a Host test would need an agent that does not answer at the start.
#   a janitors' row that is not its table's, carried: the walk's set-aside of a book's row is planted (one place for
#     both); the janitors' own `leave_out` of it says nothing more once the walk counted it.
PLANTS = [
    # The loader.
    (K, 'kept', 'a row that cannot be read is not counted', '        Policy::Rows => left.left_out(table, key, log),', '        Policy::Rows => {}'),
    (K, 'kept', 'a row that cannot be read keeps no names', '            Found::Absent | Found::Unfit => listed.names.extend(names),', '            Found::Absent | Found::Unfit => drop(names),'),
    (K, 'kept', 'a secret key is printed', 'if kind(table).is_some_and(|kind| kind.key == Key::Plain) {\n        key.to_string()', 'if kind(table).is_some_and(|kind| kind.key == Key::Plain) || !key.is_empty() {\n        key.to_string()'),
    (K, 'kept', 'a record is said every time it is read', 'keys.insert(key.to_string()).then_some(keys.len())', '{\n            keys.insert(key.to_string());\n            Some(keys.len())\n        }'),
    (K, 'kept', "the owner's write is not the repair", '        if kind(table).is_none_or(|kind| kind.policy == Policy::Rows) {\n            self.left.written(table, key, &self.log);\n        }\n        Ok(())', '        Ok(())'),
    # The fleet's start on it.
    (F, 'fleet', 'an agent that cannot be set up gives up its names', '                held.push(crate::kept::Name {', '                drop(crate::kept::Name {'),
    (F, 'fleet', 'a session held by an agent the Host does not run is free', '        let held = self.held.iter().any(|name| name.session == session);', '        let held = false;'),
    (F, 'fleet', 'a name held by an agent the Host does not run is free', '            let mut held = held.filter(|kept| roster::same_name(&kept.persona, name));', '            let mut held = held.filter(|_| false);'),
    (F, 'fleet', "a deleted agent's goals that cannot be retired stop the start", '                if retire_goals(&works, &record, at).is_err() {\n', '                if let Err(why) = retire_goals(&works, &record, at) {\n                    return Err(why);\n                }\n                if false {\n'),
    (F, 'fleet', "the fleet's writes are not watched", '    let store: Box<dyn KeyValue> = Box::new(crate::kept::Watched::new(\n        store,\n        left_out.clone(),\n        log.clone(),\n    ));', '    let store: Box<dyn KeyValue> = store;\n    let _ = &left_out;'),
    # The calls' recovery: a store that does not answer is asked again, a few times, no more.
    (O, 'opening', 'the store is asked once', 'const TRIES: u32 = 3;', 'const TRIES: u32 = 1;'),
    (O, 'opening', 'the store is asked once too often', '                tried += 1;\n                if tried >= TRIES {\n', '                tried += 1;\n                if tried > TRIES {\n'),
    # The registry, the walk and the books.
    (K, 'kept', "a book's write is taken for a repair", '        if kind(table).is_none_or(|kind| kind.policy == Policy::Rows) {\n            self.left.written(table, key, &self.log);\n        }\n        Ok(())', '        self.left.written(table, key, &self.log);\n        Ok(())'),
    (K, 'kept', "the walk leaves the rows unread", 'kinds().filter(|kind| kind.judged == Judged::AtStart) {', 'kinds().filter(|kind| kind.judged == Judged::AtStart && kind.policy == Policy::Book) {'),
    (K, 'host', "a book's record that cannot be read is not set aside", '            store.put_together(&moved)?;', '            let _ = &moved;'),
    (K, 'kept', "a book's entries that do not fit are lost at the next save", '    carried.put_back(&mut document);\n    serde_json::to_vec', '    let _ = carried;\n    serde_json::to_vec'),
    (K, 'kept', "a desk's own key that ends like a copy is taken for one", 'copy_of(&key).is_some() && left.is_left_out(table, &key) {', 'copy_of(&key).is_some() {'),
    (F, 'host', "an agent not run for want of its tool is not counted", '                left.not_run(&record.backend);\n', ''),
    (S, 'registry', "a desk keeps a kind the registry does not have", 'crate::kept::Kind::book(KIND, crate::kept::Key::Plain, fits_book).by_entries(unfit_entries)', 'crate::kept::Kind::book("no-such-kind", crate::kept::Key::Plain, fits_book).by_entries(unfit_entries)'),
    # What cannot be read is seen by no desk: a row hidden, a book's record moved aside (never over a copy), the copies
    # counted at every start and gone with their key.
    (K, 'kept', "a book's record is copied, not moved", '                (table.to_string(), key.to_string(), None),\n', ''),
    (K, 'kept', "a copy is written over", 'let aside = copy_name(key, copies(store, table, key)?.len() + 1);', 'let aside = copy_name(key, 1);'),
    (K, 'kept', "a copy is counted only at the start that made it", '    pub fn parked(&self, table: &str, copy: &str) {\n        self.add(table, copy);', '    pub fn parked(&self, table: &str, copy: &str) {\n        let _ = (table, copy);'),
    (K, 'kept', "a delete leaves its key's copies", '        counted.retain(|copy| self.left.is_left_out(table, copy));', '        counted.clear();'),
    (F, 'fleet', "a row left out is seen by a desk", '.filter(|(key, _)| !left.is_left_out(&table, key))', '.filter(|_| true)'),
    (K, 'vocabulary', "a key whose record was moved aside is answered absent", '        if left.is_left_out(table, &copy_name(key, 1)) {\n            return Ok(Found::Unfit);', '        if left.is_left_out(table, &copy_name(key, 1)) {\n            return Ok(Found::Absent);'),
    # Each kind judged by its desk's own type.
    (Q, 'quota', "the quota's text records are judged as JSON", '|| crate::kept::text(kept)', '|| false'),
    (T, 'host', "the Host's key moved aside is drawn anew", 'store.get(&table, &crate::kept::copy_name(KEY, 1)),', 'store.get(&table, "no-such-key"),'),
    (K, 'kept', "a delete takes a desk's own key that ends like a copy", '        counted.retain(|copy| self.left.is_left_out(table, copy));\n', ''),
    (K, 'kept', "an entry written over by its desk is still carried and counted", '.filter(move |key| held.is_some_and(|map| map.contains_key(*key)))', '.filter(|_| false)'),
    # The kinds judged when read: not at the start, by their desk's own type, at each read.
    (K, 'vocabulary', "the walk reads a kind judged when read", 'kinds().filter(|kind| kind.judged == Judged::AtStart) {', 'kinds() {'),
    (V, 'vocabulary', "a run of another shape is taken for one", 'crate::kept::fits::<Run>', 'crate::kept::json'),
    (V, 'vocabulary', "the vocabulary's counter is begun anew", '        crate::kept::Found::Unfit if kind == COUNTER => Err((', '        crate::kept::Found::Unfit if false => Err(('),
    (L, 'host', "a form's ledger is numbered anew", '        crate::kept::Found::Unfit => Err(broken()),', '        crate::kept::Found::Unfit => Ok(None),'),
    (P, 'host', "a place of another shape is taken for one", '    Fix::read(kept).is_some()', '    !kept.is_empty()'),
    (OM, 'registry', "a kind judged when read is read raw whole", 'app.fleet.kept_listed(kind)', 'app.fleet.kept_all(kind)'),
    (F, 'fleet', "a kind judged when read is listed unjudged", 'None => crate::kept::listed(store, &table, left, log),', 'None => store.list(&table).map(|read| crate::kept::Listed { read, names: Vec::new() }),'),
    (M, 'registry', "a kind judged when read is read raw by key", '                .kept_book(KIND, key.clone())', '                .kept(KIND, key.clone())'),
    # The Host's own door.
    (E, 'host', "the door does not say what was left out", '        "left_out": app.fleet.left_out().counts(),', '        "left_out": json!({}),'),
    (E, 'host', "the door does not say what its count covers", '        "judged_when_read": crate::kept::judged_when_read(),', '        "judged_when_read": json!([]),'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
