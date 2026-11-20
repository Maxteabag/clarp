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
}
K = 'clarp-host/src/kept.rs'
F = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   the Host's own door (`edge.rs`, `GET /host/start`): held by the Host's test in tests/host/start_tolerance.rs,
#     which this branch takes up once it stands on the main that registers that file.
#   the start line (`main.rs`): what it says is `LeftOut::line`, planted here; that it is said is the Host's test's.
PLANTS = [
    # The loader.
    (K, 'kept', 'a row that cannot be read is not counted', '                left.left_out(table, &key, log);\n', ''),
    (K, 'kept', 'a row that cannot be read keeps no names', '                rows.names.extend(Name::of(&stored));', '                let _ = Name::of(&stored);'),
    (K, 'kept', 'a push token is said whole', 'if kind_of(table) == "push" {', 'if kind_of(table) == "no such kind" {'),
    (K, 'kept', 'a record is said every time it is read', '        if new {\n', '        if new || !new {\n'),
    (K, 'kept', "the owner's write is not the repair", '        self.inner.put(table, key, value)?;\n        self.left.written(table, key, &self.log);\n', '        self.inner.put(table, key, value)?;\n'),
    # The fleet's start on it.
    (F, 'fleet', 'a record of another shape is not counted', '            Err(_) => left.left_out(table, &key, log),', '            Err(_) => {}'),
    (F, 'fleet', "an agent's record of another shape gives up its names", '            held.extend(crate::kept::Name::of(&bytes));', '            let _ = &bytes;'),
    (F, 'fleet', 'an agent that cannot be set up gives up its names', '                held.push(crate::kept::Name {', '                drop(crate::kept::Name {'),
    (F, 'fleet', 'a session held by an agent the Host does not run is free', '        let held = self.held.iter().any(|name| name.session == session);', '        let held = false;'),
    (F, 'fleet', 'a name held by an agent the Host does not run is free', '            let mut held = held.filter(|kept| roster::same_name(&kept.persona, name));', '            let mut held = held.filter(|_| false);'),
    (F, 'fleet', "a deleted agent's goals that cannot be retired stop the start", '                if retire_goals(&works, &record, at).is_err() {\n', '                if let Err(why) = retire_goals(&works, &record, at) {\n                    return Err(why);\n                }\n                if false {\n'),
    (F, 'fleet', "the fleet's writes are not watched", '    let store: Box<dyn KeyValue> = Box::new(crate::kept::Watched::new(\n        store,\n        left_out.clone(),\n        log.clone(),\n    ));', '    let store: Box<dyn KeyValue> = store;\n    let _ = &left_out;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
