#!/usr/bin/env python3
"""Migration rehearsal: upgrade a production-shaped synthetic state.sqlite.

Builds a base database with the base code's own schema (a git ref, default the
merge-base of HEAD and origin/main, or `v61`, the oldest supported shape),
fills it with synthetic rows in production proportions (about 1M state_log
rows at --scale 1), then opens it the way the Host does and runs HEAD's
migration. Fails when the migration takes longer than --budget seconds or a
table that exists on both sides loses or gains rows, or a state_log row
changes. Never touches the live database: every child process runs with HOME,
XDG_* and CLAUDE_PWA_DB inside a temporary directory, which is removed
afterwards. Prints structure, counts and timings only.

    heavy uv run --frozen --group dev python scripts/migration_rehearsal.py --base v61

To rehearse in the proportions of a real database on another machine, write
its profile first: table names, approximate row counts and average row
sizes, nothing else. The source is opened read-only (mode=ro) and is never
counted (see write_profile). On the live Host's database, ask the program
lead for a quiet slot first.

    python scripts/migration_rehearsal.py --write-profile profile.json \
        --from-db ~/.local/share/clarp/state.sqlite
    python scripts/migration_rehearsal.py --base v61 --profile profile.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Rows at --scale 1 and average bytes per row, from the shape of a live
# database on 2026-11-03 (881k state_log rows, 4.1 GB). Other tables get
# OTHER_ROWS rows.
PROFILE: dict[str, tuple[int, int]] = {
    "state_log": (1_000_000, 760),
    "messages": (900_000, 1_500),
    "tool_explanation_decisions": (270_000, 120),
    "janitor_runs": (135_000, 800),
    "janitor_demand_results": (135_000, 200),
    "turns": (120_000, 80),
    "janitor_demand_claims": (100_000, 120),
    "user_notifications": (100_000, 230),
    "tool_explanation_learned": (85_000, 150),
    "goal_events": (65_000, 9_000),
    "judgment_decisions": (60_000, 3_000),
    "prompt_admissions": (52_000, 300),
    "sse_events": (42_000, 500),
    "queued_turns": (38_000, 300),
    "turn_usage": (38_000, 150),
    "audio_bookkeeping_events": (37_000, 200),
    "background_job_events": (24_000, 200),
    "voice_events": (17_000, 200),
    "oracle_observations": (13_000, 300),
    "background_jobs": (8_500, 500),
    "agents": (1_200, 600),
}
OTHER_ROWS = 300
# Row-count changes a migration from that base makes on purpose.
INTENDED_ROW_CHANGES: dict[str, dict[str, str]] = {
    "v61": {"settings": "v62 deletes the retired settings keys the v61 shape seeds"},
}
STATE_KINDS = ("thinking", "tool", "done", "idle", "waiting", "interrupted", "compacting")

_BUILD_BASE = """
import sqlite3, sys
sys.path[:0] = [sys.argv[1] + "/server", sys.argv[1] + "/tests/unit"]
from lib import db
con = sqlite3.connect(sys.argv[2])
db._migrate(con)
if sys.argv[3] == "v61":
    from test_db_migrations import _shape_as_v61
    _shape_as_v61(con)
    con.commit()
print(con.execute("PRAGMA user_version").fetchone()[0])
"""

_MIGRATE = """
import json, sys, time
sys.path.insert(0, sys.argv[1] + "/server")
from lib import db, db_migrations
con = db._open_connection()
started = time.monotonic()
db_migrations._migrate(con)
seconds = time.monotonic() - started
print(json.dumps({"seconds": seconds, "version": con.execute("PRAGMA user_version").fetchone()[0]}))
"""


def _sandbox_env(work: pathlib.Path, db_path: pathlib.Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLARP", "CLAUDE_PWA"))}
    home = work / "home"
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        env[name] = str(home / name.lower())
    env.update(HOME=str(home), CLAUDE_PWA_DB=str(db_path), PYTHONDONTWRITEBYTECODE="1")
    return env


def _run(code: str, args: list[str], env: dict[str, str], cwd: pathlib.Path) -> str:
    done = subprocess.run([sys.executable, "-c", code, *args], env=env, cwd=cwd,
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"child failed ({done.returncode}):\n{done.stderr[-4000:]}")
    return done.stdout.strip().splitlines()[-1]


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def build_base(base: str, work: pathlib.Path, db_path: pathlib.Path, env: dict[str, str]) -> int:
    if base == "v61":
        tree = ROOT
    else:
        tree = work / "base"
        tree.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", base, "server", "tests/unit"],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    return int(_run(_BUILD_BASE, [str(tree), str(db_path), base], env, work))


def _check_values(sql: str) -> dict[str, list[object]]:
    found = {}
    for col, body in re.findall(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)", sql or "", re.I):
        found[col] = [int(v) if re.fullmatch(r"-?\d+", v) else v.strip("'\"")
                      for v in (p.strip() for p in body.split(","))]
    return found


def _value(table, col, decl, i, rows, refs, checks, pad):
    if (table, col) in refs:
        parent, pcol, pdecl, n = refs[(table, col)]
        return _value(parent, pcol, pdecl, i % n, rows, {}, {}, 0) if n else None
    if col in checks:
        return checks[col][i % len(checks[col])]
    if table == "state_log" and col == "kind":
        return STATE_KINDS[i % len(STATE_KINDS)]
    if col == "agent_id" and table != "agents":
        return _value("agents", "agent_id", "TEXT", i % rows.get("agents", 1), rows, {}, {}, 0)
    kind = (decl or "").upper()
    if "INT" in kind:
        return i + 1
    if any(t in kind for t in ("REAL", "FLOA", "DOUB")):
        return float(i)
    if "BLOB" in kind:
        return b"\0" * min(pad, 64)
    return f"{col}-{i}" + ("x" * pad if pad else "")


def fill(con: sqlite3.Connection, scale: float, payload: float,
         profile: dict[str, tuple[int, int]] | None = None,
         other_rows: int = OTHER_ROWS) -> tuple[dict[str, int], list[str]]:
    """Insert synthetic rows; returns the row counts and the tables left under half full."""
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = OFF")
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    profile = PROFILE if profile is None else profile
    rows = {t: max(1, int(profile.get(t, (other_rows, 0))[0] * scale)) for t in tables}
    for table in tables:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        if re.search(r"singleton\s+INTEGER\s+PRIMARY\s+KEY", sql, re.I):
            rows[table] = 1
    types = {(t, r[1]): r[2] for t in tables for r in con.execute(f'PRAGMA table_info("{t}")')}
    for table in tables:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        cols = [(r[1], r[2], r[5]) for r in con.execute(f'PRAGMA table_info("{table}")')]
        refs = {(table, r[3]): (r[2], r[4] or "rowid", types.get((r[2], r[4]), "INTEGER"), rows.get(r[2], 0))
                for r in con.execute(f'PRAGMA foreign_key_list("{table}")')}
        checks = _check_values(sql)
        if "(agent_id IS NULL) <> (team_id IS NULL)" in sql:
            checks["team_id"] = [None]
        text_cols = [c for c, d, pk in cols if not pk and "INT" not in (d or "").upper()
                     and (table, c) not in refs and c not in checks]
        pad_col = text_cols[0] if text_cols else None
        pad = max(0, int(profile.get(table, (0, 0))[1] * payload) - 16 * len(cols))
        rowid_pk = [c for c, d, pk in cols if pk == 1 and (d or "").upper() == "INTEGER"]
        names = [c for c, _, _ in cols if c not in rowid_pk]
        insert = f'INSERT OR IGNORE INTO "{table}" ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})'
        decl = {c: d for c, d, _ in cols}
        batch = []
        for i in range(rows[table]):
            batch.append(tuple(_value(table, c, decl[c], i, rows, refs, checks, pad if c == pad_col else 0)
                               for c in names))
            if len(batch) >= 5000:
                con.executemany(insert, batch)
                batch.clear()
        con.executemany(insert, batch)
        con.commit()
    filled = counts(con)
    return filled, sorted(t for t in tables if filled[t] < rows[t] // 2)


def counts(con: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}


def state_log_digest(con: sqlite3.Connection, columns: list[str]) -> str:
    digest = hashlib.sha256()
    query = f'SELECT {", ".join(columns)} FROM state_log ORDER BY rowid'
    for row in con.execute(query):
        digest.update(repr(row).encode())
    return digest.hexdigest()


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def write_profile(source: pathlib.Path, sample: int = 2_000) -> dict:
    """Approximate row counts and average row bytes per table of `source`.

    Cheap on purpose, because the source may be a live 4 GB database: no
    count(*). A rowid table's rows are max(rowid) - min(rowid) + 1 (an upper
    bound when rows were deleted), and its size is averaged over the newest
    `sample` rows found by a rowid seek. A WITHOUT ROWID table records only its
    first `sample` rows as a floor. Every statement is its own short read on a
    read-only connection, so no snapshot is held across tables. Ask for a quiet
    slot before pointing this at the live Host's database.
    No value leaves this function: only names, counts and sizes.
    """
    con = sqlite3.connect(f"file:{source}?mode=ro", uri=True, isolation_level=None)
    try:
        tables, methods = {}, {}
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in names:
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0] or ""
            columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            size = " + ".join(f'coalesce(length("{c}"), 0)' for c in columns) or "0"
            if re.search(r"WITHOUT\s+ROWID", sql, re.I):
                # Sizes are summed here, not counted in SQL: the floor is len() of a bounded fetch.
                sizes = [r[0] for r in con.execute(f'SELECT {size} FROM "{table}" LIMIT ?', (sample,))]
                rows, avg = len(sizes), (sum(sizes) / len(sizes) if sizes else 0)
                methods[table] = "without_rowid_sample_floor"
            else:
                # Two statements: SQLite seeks min() or max() alone, but scans for both together.
                low = con.execute(f'SELECT min(rowid) FROM "{table}"').fetchone()[0]
                high = con.execute(f'SELECT max(rowid) FROM "{table}"').fetchone()[0]
                rows = 0 if high is None else high - low + 1
                avg = con.execute(
                    f'SELECT avg(s) FROM (SELECT {size} AS s FROM "{table}" ORDER BY rowid DESC LIMIT ?)',
                    (sample,)).fetchone()[0]
                methods[table] = "rowid_span"
            tables[table] = [int(rows), int(avg or 0)]
        version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    return {"kind": "clarp-state-profile", "user_version": version, "sample_rows": sample,
            "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "row_methods": methods, "tables": tables}


PROFILE_MAX_BYTES = 1 << 20  # a profile is a few kB; anything bigger is not one


def _is_profile(path: pathlib.Path) -> bool:
    """True only for a small regular file holding a clarp-state-profile; reads at most PROFILE_MAX_BYTES."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > PROFILE_MAX_BYTES:
        return False
    with path.open("rb") as handle:
        head = handle.read(PROFILE_MAX_BYTES + 1)
    if len(head) > PROFILE_MAX_BYTES or head.startswith(b"SQLite format 3"):
        return False
    try:
        data = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("kind") == "clarp-state-profile"


def check_destination(path: pathlib.Path, source: pathlib.Path | None = None) -> pathlib.Path:
    """The expanded target, or SystemExit when writing it could touch anything but a profile."""
    path = path.expanduser()
    if source is not None:
        source = source.expanduser()
        # SQLite names the companions after the path it opens, which may be an
        # alias, and after the file it resolves to: protect both families.
        bases = {source, pathlib.Path(os.path.realpath(source))}
        companions = [b.with_name(b.name + s) for b in bases for s in ("", "-wal", "-shm", "-journal")]
        if any(os.path.realpath(path) == os.path.realpath(c) for c in companions) or (
                path.exists() and source.exists() and os.path.samefile(path, source)):
            raise SystemExit(f"refusing to overwrite {path}: it is the source database")
    if path.is_symlink():
        raise SystemExit(f"refusing to overwrite {path}: it is a symlink")
    if path.exists() and not _is_profile(path):
        raise SystemExit(f"refusing to overwrite {path}: it is not a clarp-state-profile")
    return path


def save_profile(path: pathlib.Path, profile: dict, source: pathlib.Path | None = None) -> None:
    """Write a profile without ever touching another file.

    Refuses the source database and its -wal/-shm/-journal companions, and any
    existing file that is not already a profile (checked with a bounded read).
    Stages in a new, exclusively created temp file beside the target, then
    renames it into place, so no fixed staging name can collide with anything.
    """
    path = check_destination(path, source)
    handle, staging = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(json.dumps(profile, indent=1, sort_keys=True))
        os.replace(staging, path)
    except BaseException:
        os.unlink(staging)
        raise


def load_profile(path: pathlib.Path) -> dict[str, tuple[int, int]]:
    data = json.loads(path.read_text())
    if data.get("kind") != "clarp-state-profile":
        raise SystemExit(f"{path} is not a clarp-state-profile")
    return {name: (int(rows), int(size)) for name, (rows, size) in data["tables"].items()}


def rehearse(base: str, scale: float, payload: float, budget: float, keep: bool = False,
             profile: dict[str, tuple[int, int]] | None = None) -> dict:
    work = pathlib.Path(tempfile.mkdtemp(prefix="clarp-migration-rehearsal-", dir="/var/tmp"))
    try:
        db_path = work / "state.sqlite"
        env = _sandbox_env(work, db_path)
        base_version = build_base(base, work, db_path, env)
        con = sqlite3.connect(str(db_path))
        started = time.monotonic()
        before, underfilled = fill(con, scale, payload, profile)
        fill_seconds = time.monotonic() - started
        violations: dict[str, int] = {}
        for row in con.execute("PRAGMA foreign_key_check"):
            violations[row[0]] = violations.get(row[0], 0) + 1
        log_columns = _columns(con, "state_log")
        log_before = state_log_digest(con, log_columns)
        con.close()
        result = json.loads(_run(_MIGRATE, [str(ROOT)], env, work))
        con = sqlite3.connect(str(db_path))
        after = counts(con)
        missing = [c for c in log_columns if c not in _columns(con, "state_log")]
        # A migration that drops a state_log column cannot prove the rows kept;
        # it reports them as not preserved and needs a reviewed exception.
        preserved = not missing and state_log_digest(con, log_columns) == log_before
        con.close()
        changed = {t: [before[t], after[t]] for t in before if t in after and before[t] != after[t]
                   and t not in INTENDED_ROW_CHANGES.get(base, {})}
        report = {
            "base": base, "base_version": base_version, "head_version": result["version"],
            "scale": scale, "payload": payload, "fill_seconds": round(fill_seconds, 1),
            "migrate_seconds": round(result["seconds"], 2), "budget_seconds": budget,
            "database_bytes": db_path.stat().st_size, "state_log_rows": before.get("state_log", 0),
            "rows_total": sum(before.values()), "fk_violations_before": violations,
            "dropped_tables": sorted(set(before) - set(after)),
            "added_tables": sorted(set(after) - set(before)), "row_count_changes": changed,
            "underfilled_tables": underfilled, "state_log_columns_dropped": missing, "state_log_preserved": preserved,
        }
        report["ok"] = result["seconds"] <= budget and not changed and preserved
        return report
    finally:
        if keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=None, help="git ref or v61 (default: merge-base of HEAD and origin/main)")
    parser.add_argument("--scale", type=float, default=1.0, help="row-count multiplier (1.0 = about 1M state_log rows)")
    parser.add_argument("--payload", type=float, default=1.0, help="row-size multiplier")
    parser.add_argument("--budget", type=float, default=120.0, help="maximum migration seconds")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--profile", type=pathlib.Path, help="row counts and sizes from --write-profile")
    parser.add_argument("--write-profile", type=pathlib.Path, help="write the profile of --from-db and exit")
    parser.add_argument("--from-db", type=pathlib.Path, help="database to profile (opened read-only)")
    args = parser.parse_args(argv)
    if args.write_profile:
        if not args.from_db:
            parser.error("--write-profile needs --from-db")
        check_destination(args.write_profile, args.from_db)  # refuse before reading anything
        save_profile(args.write_profile, write_profile(args.from_db.expanduser()), source=args.from_db)
        return 0
    profile = load_profile(args.profile) if args.profile else PROFILE
    base = args.base or _git("merge-base", "HEAD", "origin/main")
    estimate = sum(r * b for r, b in profile.values()) * args.scale * args.payload
    free = shutil.disk_usage("/var/tmp").free
    if free < 3 * estimate:
        print(f"refusing: /var/tmp has {free >> 20} MiB free, need {3 * int(estimate) >> 20} MiB", file=sys.stderr)
        return 2
    report = rehearse(base, args.scale, args.payload, args.budget, keep=args.keep, profile=profile)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
