"""Run the actual hook in a scratch subprocess, with a historical context oracle."""
import builtins
import json
from pathlib import Path
import sys
import time

repo, case_file, version, report_file = sys.argv[1:]
sys.path[:0] = [str(Path(repo) / 'plugin/hooks'), str(Path(repo) / 'server')]
import pwa_source_flag as hook
from lib import eventlog

case = json.loads(Path(case_file).read_text())
header = 'NARRATION TEST GROUP HEADER'
hook._group_call_header = lambda agent: header
if version == 'main':
    namespace = vars(hook).copy()
    exec((Path(repo) / 'tests/fixtures/pwa_narration_main_context.py').read_text(), namespace)
    hook._build_additional_context = namespace['_build_additional_context']
if 'resolved_level' in case:
    resolve = hook._agents.resolve_for_hook
    def resolved(**kwargs):
        row = resolve(**kwargs)
        if row is not None:
            row = dict(row)
            if case['resolved_level'] == 'missing':
                row.pop('voice_verbosity', None)
            else:
                row['voice_verbosity'] = case['resolved_level']
        return row
    hook._agents.resolve_for_hook = resolved

reports = []
def private_emit(source, event, **fields):
    reports.append({'source': source, 'event': event, **fields})
if not case.get('real_storage'):
    # Existing payload tests explicitly opt their collector cases into capture.
    # The real-storage regression leaves capture policy and writers untouched.
    from lib import diagnostics_settings
    diagnostics_settings.update({'enabled': True, 'categories': ['agents']})
    eventlog.emit = private_emit
if case.get('unavailable'):
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'lib.voice_verbosity':
            raise ImportError('test: narration unavailable')
        return original_import(name, *args, **kwargs)
    builtins.__import__ = unavailable
marker = hook.PATHS.source_marker('narration-test')
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text(f'pwa-voice narration-test {time.time():.3f} narration-trace 1\n')
try:
    status = hook.main()
finally:
    Path(report_file).write_text(json.dumps(reports))
raise SystemExit(status)
