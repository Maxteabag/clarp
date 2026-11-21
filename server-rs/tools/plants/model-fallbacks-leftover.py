#!/usr/bin/env python3
"""Prove that the Host crash test detects a surviving fallback fixture."""

import runner

RUNS = {
    'host': ['test', '--test', 'host', '--',
             'inherited_models_retry_only_the_isolated_question_and_record_the_actual_model', '--exact'],
}

PLANTS = [
    ('clarp-host/tests/host.rs', 'host', 'the slow fixture keeps waiting after its Host dies',
     '        if os.getppid()!=parent:sys.exit(0)\n', ''),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
