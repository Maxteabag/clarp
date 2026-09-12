"""Read-only frontend revision tracking for the development preview."""
import hashlib
import json


def revisions(root):
    files = [root/'static/viz.html', root/'static/viz-world-host.js', root/'static/lib/avatar.js']
    files += list((root/'static/lib').glob('viz-*'))
    for view in ('world', 'flow', 'cabinets'):
        files += list((root/f'static/viz-{view}').glob('*'))
    groups = {'styles': [], 'code': []}
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        groups['styles' if path.suffix == '.css' else 'code'].append((str(path.relative_to(root)),stat.st_mtime_ns,stat.st_size))
    return {key: hashlib.sha256(json.dumps(value).encode()).hexdigest() for key,value in groups.items()}
