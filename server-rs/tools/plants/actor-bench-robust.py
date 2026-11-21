#!/usr/bin/env python3
"""The coordination benchmark must catch a scan in marked send acceptance."""

import runner

RUNS = {
    'costs': ['test', '-p', 'clarp-actor', '--test', 'actor', '--',
              'coordination_costs_the_same_in_a_conversation_of_300_000_rows_as_in_one_of_3_000', '--exact'],
}

PLANTS = [
    ('clarp-actor/src/lib.rs', 'costs', 'a caller-named trace is found by scanning the log',
     '''        let Some(stored) = self.recorder.log().find(&trace_key(trace))? else {
            return Ok(None);
        };''',
     '''        let key = trace_key(trace);
        let log = self.recorder.log();
        let mut after = 0;
        let mut stored = None;
        loop {
            let page = log.read_after(after, clarp_ports::log::Kinds::ALL, 8192)?;
            let Some(last) = page.last() else { break; };
            after = last.seq;
            if let Some(found) = page.into_iter().rev().find(|item| item.key.as_deref() == Some(key.as_str())) {
                stored = Some(found);
            }
        }
        let Some(stored) = stored else { return Ok(None); };'''),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
