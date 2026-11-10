"""Synthetic fixed-delta inputs and isolated full/candidate RM-N14 oracle.

No production fast path exists yet. The candidate entry is the production
import_transcript; the independent reference explicitly parses/stores in full.
Timing excludes process startup and the outside-the-import database oracle.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
import pathlib
import resource
import selectors
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKENDS = ("claude", "codex")
BASE_MS = 1_791_706_800_000
# Optimization metadata is checked separately, never allowed to hide a change
# to product rows or any automation/team/Oracle side-effect table.
METADATA_TABLES = frozenset({"transcript_import_checkpoints"})


def append_bytes(backend: str, ordinal: int, *, size: int = 1024,
                 long_turn: bool = False) -> bytes:
    """Exactly size bytes, complete records; a long turn has only one prompt."""
    import datetime as dt
    stamp = (dt.datetime.fromtimestamp((BASE_MS + ordinal * 1000) / 1000, dt.timezone.utc)
             .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    call, turn = f"c{ordinal}", "t0" if long_turn else f"t{ordinal}"
    records = []
    if backend == "claude":
        def record(role, content):
            return {"type": role, "timestamp": stamp,
                    "message": {"role": role, "content": content}}
        if not long_turn or ordinal == 0:
            records.append(record("user", f"synthetic request {ordinal}"))
        records += [record("assistant", [
            {"type": "text", "text": f"synthetic step {ordinal}"},
            {"type": "tool_use", "id": call, "name": "Bash", "input": {"command": "printf x"}}]),
            record("user", [{"type": "tool_result", "tool_use_id": call, "content": "x"}])]
        if not long_turn:
            records.append(record("assistant", [{"type": "text", "text": f"synthetic answer {ordinal}"}]))
    elif backend == "codex":
        def record(kind, payload):
            return {"timestamp": stamp, "type": kind, "payload": payload}
        if not long_turn or ordinal == 0:
            records += [record("event_msg", {"type": "task_started", "turn_id": turn}),
                        record("event_msg", {"type": "user_message", "message": f"synthetic request {ordinal}"})]
        records += [record("event_msg", {"type": "agent_message", "message": f"synthetic step {ordinal}", "phase": "commentary"}),
                    record("response_item", {"type": "function_call", "name": "exec_command", "call_id": call,
                                             "arguments": '{"cmd":"printf x"}', "turn_id": turn}),
                    record("response_item", {"type": "function_call_output", "call_id": call,
                                             "output": "exit: 0", "turn_id": turn})]
        if not long_turn:
            records.append(record("event_msg", {"type": "agent_message", "message": f"synthetic answer {ordinal}", "phase": "final_answer"}))
    else:
        raise ValueError("unsupported synthetic backend")
    records[-1]["synthetic_padding"] = ""
    encode = lambda: b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in records)
    remaining = size - len(encode())
    if remaining < 0:
        raise ValueError("requested append too small for complete provider records")
    records[-1]["synthetic_padding"] = "x" * remaining
    return encode()


def write_history(path: pathlib.Path, backend: str, *, mebibytes: int,
                  long_turn: bool = False) -> int:
    batches = mebibytes * 1024
    with path.open("wb") as output:
        for ordinal in range(batches):
            output.write(append_bytes(backend, ordinal, long_turn=long_turn))
    return batches


def database_view(path: pathlib.Path, *, metadata: bool = False) -> dict:
    """Read every product table, including side effects, without exclusions by area."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        names = [r[0] for r in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') ORDER BY name")]
        view = {}
        for name in names:
            if (name in METADATA_TABLES) != metadata:
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = database.execute(f"SELECT * FROM {quoted}")
            rows = [[{"blob_hex": v.hex()} if isinstance(v, bytes) else v for v in r] for r in cursor]
            rows.sort(key=lambda r: json.dumps(r, sort_keys=True, separators=(",", ":")))
            view[name] = {"columns": [c[0] for c in cursor.description], "rows": rows}
        return view


def assert_product_equal(reference: pathlib.Path, candidate: pathlib.Path) -> None:
    left, right = database_view(reference), database_view(candidate)
    differing = sorted(k for k in left.keys() | right.keys() if left.get(k) != right.get(k))
    if differing:
        # Never include row contents in a diagnostic, even for synthetic data.
        counts = [{"table": k, "reference_rows": len(left.get(k, {}).get("rows", [])),
                   "candidate_rows": len(right.get(k, {}).get("rows", []))} for k in differing]
        for item in counts:
            if item["table"] != "messages" or left["messages"]["columns"] != right["messages"]["columns"]:
                continue
            columns = left["messages"]["columns"]
            primary = columns.index("message_id")
            a = {row[primary]: row for row in left["messages"]["rows"]}
            b = {row[primary]: row for row in right["messages"]["rows"]}
            if a.keys() == b.keys():
                item["column_change_counts"] = {name: sum(a[key][i] != b[key][i] for key in a)
                                               for i, name in enumerate(columns)
                                               if any(a[key][i] != b[key][i] for key in a)}
        raise AssertionError(json.dumps({"code": "product_state_differs", "tables": counts}))



def assert_checkpoint_parse_equal(database: pathlib.Path, transcript: pathlib.Path,
                                  backend: str) -> dict:
    """Audit durable parse coverage separately from product equality.

    A full parse is the reference for the range parser and its boundary.
    This does not certify store carry or external effects; those need replay.
    Diagnostics contain codes/counts, never rows or carried values.
    """
    from lib import backends
    from lib import transcript_incremental as inc
    from tests.readmodel.corpus import AGENT, SESSION, SENDER
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        checkpoint = inc.load_checkpoint(connection, AGENT, SESSION, str(transcript))
    if checkpoint is None:
        raise AssertionError("checkpoint_missing")
    cp = checkpoint
    size = transcript.stat().st_size
    if not 0 <= cp.boundary_offset <= cp.complete_size <= cp.size <= size:
        raise AssertionError("checkpoint_byte_watermark")
    if cp.complete_size != inc.complete_size(transcript, cp.size):
        raise AssertionError("checkpoint_complete_record")
    if cp.parser_version != inc.parser_version(backend) or cp.backend != backend:
        raise AssertionError("checkpoint_parser_version")
    if (cp.device, cp.inode) != (transcript.stat().st_dev, transcript.stat().st_ino):
        raise AssertionError("checkpoint_file_identity")
    if cp.prefix_hash != inc.prefix_hash(transcript, cp.boundary_offset):
        raise AssertionError("checkpoint_prefix_bytes")
    if cp.tail_hash != inc.range_hash(transcript, cp.boundary_offset, cp.complete_size):
        raise AssertionError("checkpoint_tail_bytes")
    turns = backends.parse_turns(backend, transcript)
    if not 0 <= cp.boundary_seq < cp.turn_count == len(turns):
        raise AssertionError("checkpoint_turn_watermark")
    if cp.prefix_turns_hash != inc.prefix_turns_hash(inc.turn_digests(turns), cp.boundary_seq):
        raise AssertionError("checkpoint_prefix_turns")
    if inc.parse_range(backend, transcript, cp.boundary_offset, cp.size) != turns[cp.boundary_seq:]:
        raise AssertionError("checkpoint_tail_parse")
    return {"complete_bytes": cp.complete_size, "boundary_seq": cp.boundary_seq,
            "turn_count": cp.turn_count, "store_carry_certified": False}


@contextmanager
def observe_source_reads(path: pathlib.Path, metrics: dict):
    """Delivered bytes from this source, including hash/sample/range readers.

    Test instrumentation only: never a physical IO/SQLite/whole-ROOT budget
    certificate. New low-level readers need explicit coverage before GREEN.
    """
    import builtins
    import io
    target = os.path.abspath(os.fspath(path))
    stat = path.stat()
    source_identity = (stat.st_dev, stat.st_ino)
    metrics["source_read_bytes"] = 0

    def source_fd(fd):
        try:
            stat = os.fstat(fd)
            return (stat.st_dev, stat.st_ino) == source_identity
        except (OSError, TypeError):
            return False

    def charge(value):
        if isinstance(value, str):
            metrics["source_read_bytes"] += len(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray, memoryview)):
            metrics["source_read_bytes"] += len(value)
        elif isinstance(value, list):
            for item in value:
                charge(item)
        return value

    class ObservedFile:
        def __init__(self, native):
            self.native = native

        def __getattr__(self, name):
            value = getattr(self.native, name)
            if name in ("buffer", "raw"):
                return ObservedFile(value)
            if name in ("read", "read1", "readall", "readline", "readlines"):
                def read(*args, **kwargs):
                    return charge(value(*args, **kwargs))
                return read
            if name in ("readinto", "readinto1"):
                def readinto(*args, **kwargs):
                    count = value(*args, **kwargs)
                    metrics["source_read_bytes"] += count or 0
                    return count
                return readinto
            return value

        def __enter__(self):
            self.native.__enter__()
            return self

        def __exit__(self, *args):
            return self.native.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            return charge(next(self.native))

    def observe(opening):
        def opened(file, *args, **kwargs):
            native = opening(file, *args, **kwargs)
            try:
                matches = os.path.abspath(os.fspath(file)) == target
            except TypeError:
                matches = source_fd(file)
            return ObservedFile(native) if matches else native
        return opened

    def observe_fd(reading):
        def read(fd, *args, **kwargs):
            matches = source_fd(fd)
            value = reading(fd, *args, **kwargs)
            return charge(value) if matches else value
        return read

    with ExitStack() as stack:
        stack.enter_context(patch.object(builtins, "open", observe(builtins.open)))
        stack.enter_context(patch.object(io, "open", observe(io.open)))
        stack.enter_context(patch.object(os, "read", observe_fd(os.read)))
        stack.enter_context(patch.object(os, "pread", observe_fd(os.pread)))
        yield


def run_lane(database: pathlib.Path, transcript: pathlib.Path, backend: str,
             lane: str, *, tick: int, initialize: bool = True,
             close: bool = True, faults: dict | None = None) -> dict:
    """Run a tick, with either fresh-process or persistent-process isolation."""
    sys.path[:0] = [str(ROOT), str(ROOT / "server")]
    from lib import backends, db, eventlog, message_store, message_writes, telemetry
    from lib import transcript_incremental as inc
    from tests.readmodel.corpus import AGENT, SESSION, SENDER
    if initialize:
        db.reset_for_tests(database)
        telemetry.reset_for_tests(database.with_suffix(".telemetry.sqlite"))
        eventlog.reset_for_tests(database.parent / (database.stem + "-logs"))
    metrics = {"lane": lane, "backend": backend, "tick": tick,
               "process_id": os.getpid(), "source_bytes": transcript.stat().st_size,
               "components": {}, "queries": {}, "probe_sql_s": 0.0,
               "locked_sql_s": 0.0, "writer_hold_s": 0.0, "writer_max_hold_s": 0.0,
               "profiling": "inclusive wrappers and SQL trace; startup and oracle excluded"}
    connection = db.conn()

    def counted(sql):
        verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "EMPTY"
        metrics["queries"][verb] = metrics["queries"].get(verb, 0) + 1

    def timed(name, original):
        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                metric = metrics["components"].setdefault(name, {"status": "measured", "calls": 0, "seconds": 0.0})
                metric["calls"] += 1
                metric["seconds"] += time.perf_counter() - started
        return call

    execute, commit = message_writes._ImportWriter.execute, message_writes._ImportWriter.commit

    def measured_execute(writer, *args, **kwargs):
        started = time.perf_counter()
        try:
            return execute(writer, *args, **kwargs)
        finally:
            key = "locked_sql_s" if writer.locked else "probe_sql_s"
            metrics[key] += time.perf_counter() - started

    def measured_commit(writer):
        began = writer.began_at
        result = commit(writer)
        if began is not None:
            held = time.monotonic() - began
            metrics["writer_hold_s"] += held
            metrics["writer_max_hold_s"] = max(metrics["writer_max_hold_s"], held)
        return result

    try:
        with ExitStack() as stack:
            if (faults or {}).get("source_read_counter"):
                stack.enter_context(observe_source_reads(transcript, metrics))
            stack.enter_context(patch("time.time", return_value=(BASE_MS + tick * 1000) / 1000))
            stack.enter_context(patch.object(inc, "settings", return_value=dict(inc._DEFAULTS,
                enabled=lane == "checkpoint", sample_every=1, max_cpu_seconds_per_hour=1e9,
                max_load_per_cpu=1e9, max_io_pressure=1e9, max_memory_pressure=1e9)))
            for name in ("_row_phases", "_visible_replies", "_prepare_import_prelude", "_revalidate_import_prelude"):
                if hasattr(message_writes, name):
                    stack.enter_context(patch.object(message_writes, name, timed(name, getattr(message_writes, name))))
                else:
                    metrics["components"][name] = {"status": "UNMEASURED", "calls": None, "seconds": None}
            stack.enter_context(patch.object(message_writes._ImportWriter, "execute", measured_execute))
            stack.enter_context(patch.object(message_writes._ImportWriter, "commit", measured_commit))
            faults = faults or {}
            from lib import log as host_log
            original_log = host_log.log
            metrics["tail_events"] = []

            def capture_log(event, detail=""):
                if event in ("transcriptTailDecision", "transcriptTailRetention", "transcriptTailFenceRead"):
                    import re
                    shape = r"code=[a-z0-9_]+ count=[1-9][0-9]*"
                    if event == "transcriptTailDecision":
                        shape += (r"(?: reason=(?:evicted_empty|evicted|overweight|unknown|consumed|unknown_concurrent|revision|unknown_revision|unknown_removal|unknown_metadata|unknown_reset|missing_unknown|publication_foreign_commit_yield|publication_connection_changed_yield|publication_yield_without_release_sample|publication_data_version_read_failure|publication_refused_unknown|publication_not_written|publication_file_unavailable|publication_file_moved|publication_superseded|oracle_history|automation_provenance|bootstrap_effect|bootstrap_file_moved|bootstrap_empty|bootstrap_assistant_prefix|bootstrap_partial_record|bootstrap_io|bootstrap_refused))?"
                                  r" backend=(?:claude|codex|agy|other)"
                                  r" size=(?:unknown|lt1m|lt16m|lt64m|lt256m|lt1024m|ge1024m)")
                    assert re.fullmatch(shape, detail)
                    fields = dict(item.split("=", 1) for item in detail.split())
                    metrics["tail_events"].append({"event": event, "code": fields["code"],
                                                   "count": int(fields["count"])})
                    if "reason" in fields:
                        assert event == "transcriptTailDecision" and fields["code"] == "full_no_frontier"
                        metrics["tail_events"][-1]["reason"] = fields["reason"]
                return original_log(event, detail)

            stack.enter_context(patch.object(host_log, "log", capture_log))
            if faults.get("fence_read_at"):
                original_init = message_writes._ImportWriter.__init__
                read_at = faults["fence_read_at"]

                class FenceReadFailure:
                    def __init__(self, native):
                        self.native, self.reads = native, 0

                    def execute(self, sql, parameters=()):
                        if str(sql).strip().upper() == "PRAGMA DATA_VERSION":
                            self.reads += 1
                            metrics["fence_read_attempts"] = self.reads
                            if self.reads == read_at:
                                raise sqlite3.OperationalError("synthetic version read failure")
                        return self.native.execute(sql, parameters)

                    def __getattr__(self, name):
                        return getattr(self.native, name)

                def with_read_fault(writer, native, *, locked):
                    original_init(writer, native, locked=locked)
                    if locked:
                        writer._database = FenceReadFailure(native)

                stack.enter_context(patch.object(message_writes._ImportWriter, "__init__", with_read_fault))
            if faults.get("checkpoint"):
                original_checkpoint = inc.write_checkpoint

                def broken_checkpoint(*args, **kwargs):
                    original_checkpoint(*args, **kwargs)
                    raise sqlite3.OperationalError("synthetic progress failure")

                stack.enter_context(patch.object(inc, "write_checkpoint", broken_checkpoint))
            if faults.get("team"):
                from lib import team_store
                original_capture = team_store.capture_assistant_message

                def broken_capture(**kwargs):
                    if team_store.extract_team_blocks(kwargs.get("text")):
                        raise sqlite3.OperationalError("synthetic fanout failure")
                    return original_capture(**kwargs)

                stack.enter_context(patch.object(team_store, "capture_assistant_message", broken_capture))
            if faults.get("client_on_yield") or faults.get("connection_on_yield"):
                original_yield = message_writes._ImportWriter.yield_writer
                injected = False
                metrics["yield_windows"] = 0

                def intervene_during_yield(writer):
                    nonlocal injected
                    original_yield(writer)
                    metrics["yield_windows"] += 1
                    target_seq = faults.get("yield_before_seq")
                    selected = (writer.seq == target_seq if target_seq is not None
                                else metrics["yield_windows"] == faults.get("yield_at", 1))
                    if injected or not selected:
                        return
                    assert not writer.in_transaction
                    if faults.get("connection_on_yield"):
                        replacement = sqlite3.connect(database, isolation_level=None)
                        replacement.row_factory = sqlite3.Row
                        replacement.set_trace_callback(counted)
                        stack.callback(replacement.close)
                        stack.callback(replacement.set_trace_callback, None)
                        writer._database = replacement
                    else:
                        with sqlite3.connect(database) as other:
                            revision = other.execute(
                                "UPDATE message_clock SET revision=revision+1 WHERE singleton=0 RETURNING revision"
                            ).fetchone()[0]
                            other.execute(
                                "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,"
                                "timestamp,text,updated_at,revision,origin,sender_agent_id,trace_id) "
                                "VALUES ('u-interleaved',?,?, 'client:interleaved',-1000,'user',"
                                "'2026-10-11T00:00:00Z','synthetic request 0',?,?, 'agent',?, 'interleaved-prefix')",
                                (AGENT, SESSION, BASE_MS + tick * 1000, revision, SENDER))
                    injected = True
                    metrics["yield_intervention"] = metrics["yield_windows"]

                stack.enter_context(patch.object(message_writes._ImportWriter, "yield_writer", intervene_during_yield))
            if faults.get("single_row_batches"):
                stack.enter_context(patch.object(message_store, "IMPORT_COMMIT_EVERY", 1))
                stack.enter_context(patch.object(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9))
                stack.enter_context(patch.object(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0))
            connection.set_trace_callback(counted)
            started = time.perf_counter()
            if lane == "reference":
                parsed_at = time.perf_counter()
                turns = backends.parse_turns(backend, transcript)
                metrics["parse_s"] = time.perf_counter() - parsed_at
                stored_at = time.perf_counter()
                message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                                     source_file=str(transcript), turns=turns)
                metrics.update(rows=len(turns), store_s=time.perf_counter() - stored_at)
            elif lane in ("candidate", "checkpoint"):
                metrics.update(inc.import_transcript(backend=backend, path=transcript,
                                                    agent_id=AGENT, backend_session_id=SESSION,
                                                    **({"parse": backends.by_id(backend).parse_transcript}
                                                       if faults.get("native_bound_parser") else {})))
            else:
                raise ValueError("unknown oracle lane")
            metrics["total_s"] = time.perf_counter() - started
            metrics["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            metrics["candidate_fast_path_proven"] = metrics.get("apply_mode") == "tail"
            if lane in ("candidate", "checkpoint"):
                decisions = [e for e in metrics["tail_events"] if e["event"] == "transcriptTailDecision"]
                assert len(decisions) == 1 and decisions[0]["count"] == 1
        return metrics
    finally:
        connection.set_trace_callback(None)
        if close:
            db.close_local()


def isolated_lane(database: pathlib.Path, transcript: pathlib.Path, backend: str,
                  lane: str, *, tick: int, timeout: float = 120) -> dict:
    report = database.with_suffix(f".{lane}-{tick}.json")
    result = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()),
                             "--database", str(database), "--transcript", str(transcript),
                             "--backend", backend, "--lane", lane, "--tick", str(tick),
                             "--report", str(report)], capture_output=True, timeout=timeout)
    if result.returncode:
        database.with_suffix(".worker-error.log").write_bytes(result.stderr)
        raise RuntimeError(f"oracle worker failed code={result.returncode} lane={lane}")
    return json.loads(report.read_text())


class PersistentLane:
    """An owned child per lane: no module/connection reset between ticks."""
    def __init__(self, database, transcript, backend, lane, *, timeout=120):
        self.timeout = timeout
        self.error_log = database.with_suffix(f".{lane}.stderr").open("wb")
        self.process = subprocess.Popen([
            sys.executable, str(pathlib.Path(__file__).resolve()), "--persistent",
            "--database", str(database), "--transcript", str(transcript),
            "--backend", backend, "--lane", lane],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.error_log)
        self.buffer = b""

    def __enter__(self):
        return self

    def tick(self, tick, *, faults=None, close_connection=False):
        self.process.stdin.write(json.dumps({"tick": tick, "faults": faults,
                                             "close_connection": close_connection}).encode() + b"\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        if b"\n" not in self.buffer:
            with selectors.DefaultSelector() as ready:
                ready.register(self.process.stdout, selectors.EVENT_READ)
                while b"\n" not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not ready.select(remaining):
                        raise TimeoutError("persistent oracle worker deadline")
                    part = os.read(self.process.stdout.fileno(), 65536)
                    if not part:
                        raise RuntimeError(f"persistent oracle worker exited code={self.process.poll()}")
                    self.buffer += part
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def __exit__(self, *exc):
        try:
            self.process.communicate(input=b'{"stop":true}\n', timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError) as error:
            print(f"oracleWorkerShutdown code={type(error).__name__}", file=sys.stderr)
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(2)
        finally:
            self.error_log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=pathlib.Path, required=True)
    parser.add_argument("--transcript", type=pathlib.Path, required=True)
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    parser.add_argument("--lane", choices=("reference", "candidate", "checkpoint"), required=True)
    parser.add_argument("--tick", type=int)
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--persistent", action="store_true")
    args = parser.parse_args()
    if args.persistent:
        first = True
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("stop"):
                break
            try:
                result = run_lane(args.database, args.transcript, args.backend, args.lane,
                                  tick=request["tick"], initialize=first, close=request.get("close_connection", False),
                                  faults=request.get("faults"))
            except Exception as error:
                print(f"oracleTickFailed code={type(error).__name__}", file=sys.stderr)
                result = {"failure_code": type(error).__name__, "process_id": os.getpid()}
            first = False
            print(json.dumps(result), flush=True)
    else:
        if args.tick is None or args.report is None:
            parser.error("single tick needs --tick and --report")
        args.report.write_text(json.dumps(run_lane(args.database, args.transcript, args.backend,
                                                  args.lane, tick=args.tick), indent=2) + "\n")
