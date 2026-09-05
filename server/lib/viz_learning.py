"""One bounded cold worker per process. HTTP only offers work; never awaits it."""
import threading
import time
from collections import OrderedDict

from . import viz_library, viz_rule_author

_lock = threading.Lock()
_pending = OrderedDict()
_failed_until = {}
_active = ''
_running = False


def offer(clusters: list[dict]) -> dict:
    global _running
    with _lock:
        for cluster in clusters:
            exe = cluster['hint']
            if not viz_library.EXE.fullmatch(exe):
                continue
            if (exe != _active and exe not in _pending and len(_pending) < 40
                    and (cluster.get('supersede') or _failed_until.get(exe, 0) < time.monotonic())
                    and cluster['count'] > cluster.get('clamped', 0)):
                _pending[exe] = cluster
        if _pending and not _running:
            _running = True
            threading.Thread(target=_work, name='viz-author', daemon=True).start()
        return {'designing': _active, 'queued': list(_pending)}


def status() -> dict:
    with _lock:
        return {'designing': _active, 'queued': list(_pending)}


def _work():
    global _active, _running
    while True:
        with _lock:
            if not _pending:
                _active = ''
                _running = False
                return
            _active, cluster = _pending.popitem(last=False)
        try:
            result = viz_rule_author.learn([cluster], limit=1, supersede=cluster.get('supersede'))
        except Exception as error:
            result = {'rejected': [{'error': str(error)}]}
        with _lock:
            if result['rejected']:
                _failed_until[_active] = time.monotonic() + 600
                if len(_failed_until) > 500:
                    _failed_until.pop(next(iter(_failed_until)))
            _active = ''


def supersede(entity_id: str, revision: int, reason: str) -> dict:
    library = viz_library.load()
    if library['revision'] != revision:
        raise ValueError('library changed; refresh the map')
    if entity_id not in library['entities']:
        raise ValueError('unknown entity')
    previous = next((d for d in reversed(library['decisions'])
                     if d['design']['entity']['id'] == entity_id), None)
    rule = next((r for r in library['rules'].values() if r['target'] == entity_id), None)
    if not rule:
        raise ValueError('entity has no authorable rule')
    decision = previous['id'] if previous else 'seed:' + rule['exe']
    return offer([{'hint': rule['exe'], 'count': 1, 'clamped': 0,
                   'example': json_reason(rule, reason), 'supersede': decision}])


def json_reason(rule: dict, reason: str) -> str:
    import json
    return json.dumps({'existing_rule': rule, 'reason': reason[:500]})
