"""Fast workspace lookup from zoxide, recent launches and explicit paths."""
from pathlib import Path
import subprocess
from . import db


def lookup(query="", *, home=None, zoxide="zoxide"):
    home = Path(home or Path.home())
    query = str(query).strip()[:512]
    recent = [r[0] for r in db.conn().execute(
        """SELECT p.path FROM path_usage p
           WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.cwd=p.path AND a.is_janitor=1)
              OR EXISTS (SELECT 1 FROM agents a WHERE a.cwd=p.path AND a.is_janitor=0)
           ORDER BY p.last_used_at DESC, p.path LIMIT 30""")]
    def fuzzy(path):
        remaining = iter(str(path).casefold())
        return all(character in remaining for character in query.casefold().replace(" ", ""))
    ranked = []
    try:
        result = subprocess.run([zoxide, "query", "--list", "--", *query.split()],
                                capture_output=True, text=True, timeout=0.5)
        if result.returncode == 0:
            ranked = result.stdout.splitlines()[:100]
        elif query:
            all_paths = subprocess.run([zoxide, "query", "--list"], capture_output=True, text=True, timeout=0.5)
            ranked = [path for path in all_paths.stdout.splitlines()[:1000] if fuzzy(path)][:100]
    except (OSError, subprocess.TimeoutExpired):
        pass
    explicit = []
    if query:
        value = str(home) + query[1:] if query.startswith("~") else query
        path = Path(value)
        if not path.is_absolute(): path = home / path
        if path.is_dir(): explicit.append(str(path))
        try:
            parent = path if query.endswith("/") else path.parent
            prefix = "" if query.endswith("/") else path.name.casefold()
            explicit += [str(p) for p in sorted(parent.iterdir())
                         if p.is_dir() and p.name.casefold().startswith(prefix)][:30]
        except OSError:
            pass
    path_query = query.startswith(("/", "~", ".")) or "/" in query
    candidates = ([str(home)] + recent + ranked) if not query else (
        (explicit + ranked if path_query else ranked + explicit) + [p for p in recent if fuzzy(p)])
    seen = set(); matches = []
    for value in candidates:
        path = Path(value).expanduser()
        key = str(path)
        if key in seen or not path.is_dir(): continue
        seen.add(key)
        label = "~" + key[len(str(home)):] if key == str(home) or key.startswith(str(home) + "/") else key
        matches.append({"path": key, "label": label})
        if len(matches) == 16: break
    return {"home": str(home), "query": query, "matches": matches}
