"""One-node passive pytest observation. No replacement of product/test calls.

Loaded original code is compared with code compiled from pinned source, never
executed by this observer. RETURN_VALUE/RETURN_CONST distinguish actual normal
return from the trace 'return' event emitted during exception unwinding.
No arguments, return payloads, SQL parameters, DB/config/HOME bytes are emitted.
"""
import dis
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

NODE = "tests/unit/test_janitor_hotseat.py::test_receipt_writer_never_builds_full_janitor_views"
TEST = "tests/unit/test_janitor_hotseat.py"
PINS = {'tests/unit/test_janitor_hotseat.py': 'afb5e5a20aeb8859d46e40623db108ee04f6bcf970635b032c2b813c9f8d761a', 'server/lib/janitor_autonomy.py': 'e1ab9c90a5488c28f1087c1a90132789a936450c4087da2ac856d7421a9e2b0c', 'server/lib/janitor_builtins.py': 'd397e7aa67216f9fac8ba06dac05d60d7d55d65d3573b9b9bf1fdd559c112fa5', 'server/lib/janitors.py': '3e28dd88910af9c863d6b9a671af27771dda51ed68588c434e6c153e1fb0af36', 'server/lib/settings_store.py': 'af2060e9ef0b0293c5120151cb0860ffff67466214aed808324d5fcc87912d75', 'server/lib/db.py': '9f6007107f7c6cac839355f387e6bdad85272b8adaf1063dcaa154217abf6a6a'}
WATCH = {
    "server/lib/janitor_autonomy.py": {"AutonomyJanitors.run_once", "AutonomyJanitors.quota_once", "_automatic_hotseat_owns_quota"},
    "server/lib/janitor_builtins.py": {"resolve", "eligible"},
    "server/lib/janitors.py": {"get", "_execution", "_model", "_cadence"},
    "server/lib/settings_store.py": {"transaction"},
    "server/lib/db.py": {"_TrackedConnection.execute"},
}
ROOT = Path.cwd().resolve()
state = {"node": NODE, "events": [], "reports": [], "loaded": [], "diagnostic_errors": [],
         "line527": False, "line528": False, "line529": False, "qualified": False,
         "closure": "ephemeral_Actions_VM_teardown; in_job_census_informational"}
expected, watched, calls, begins = {}, {}, {}, {}
old_trace = None
enabled = False


def fingerprint(code):
    def value(x):
        if isinstance(x, types.CodeType):
            return ["code", structure(x)]
        if isinstance(x, tuple):
            return ["tuple", [value(v) for v in x]]
        return [type(x).__name__, repr(x)]
    def structure(c):
        return [c.co_name, c.co_qualname, c.co_filename, c.co_firstlineno, c.co_code.hex(),
                c.co_linetable.hex(), c.co_exceptiontable.hex(), c.co_flags,
                c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount, c.co_nlocals,
                c.co_stacksize, c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars,
                [value(v) for v in c.co_consts]]
    return hashlib.sha256(json.dumps(structure(code), ensure_ascii=True).encode()).hexdigest()


def descend(code):
    yield code
    for item in code.co_consts:
        if isinstance(item, types.CodeType):
            yield from descend(item)


def append(row):
    if len(state["events"]) >= 2048:
        if "event_limit" not in state["diagnostic_errors"]:
            state["diagnostic_errors"].append("event_limit")
        return None
    row["seq"] = len(state["events"])
    state["events"].append(row)
    return row


def stack(frame):
    while frame is not None:
        yield frame
        frame = frame.f_back


def normal_return(frame):
    op = {i.offset: i.opname for i in dis.get_instructions(frame.f_code)}
    return op.get(frame.f_lasti) in {"RETURN_VALUE", "RETURN_CONST"}


def observe(frame, event, arg):
    # This observer runs on the test thread only. No blanket thread join, tracing
    # injection into other threads, callback replacement or swallowed control.
    try:
        code = frame.f_code
        path = Path(code.co_filename)
        if path == ROOT / TEST and code.co_name == NODE.split("::")[1]:
            if event == "line" and frame.f_lineno in (527, 528, 529):
                state["line" + str(frame.f_lineno)] = True
            if event == "exception" and frame.f_lineno == 528:
                state["assert528_exception"] = arg[0] is AssertionError
        if event == "call":
            try:
                relative = str(path.relative_to(ROOT))
            except ValueError:
                return observe
            if relative not in WATCH or code.co_qualname not in WATCH[relative]:
                return observe
            digest = fingerprint(code)
            if digest != expected.get((relative, code.co_qualname)):
                state["diagnostic_errors"].append("loaded_code_mismatch:" + relative + ":" + code.co_qualname)
                return observe
            if code not in watched:
                watched[code] = True
                state["loaded"].append({"path": relative, "qualname": code.co_qualname,
                                        "code_sha256": digest, "source_sha256": PINS[relative]})
            # Only this test's original receipt_transaction -> settings-store
            # transaction -> actual SQLite execute qualifies a BEGIN.
            chain = list(stack(frame))
            receipt = any(f.f_code.co_name == "receipt_transaction" and Path(f.f_code.co_filename) == ROOT / TEST for f in chain)
            if relative == "server/lib/db.py" and frame.f_locals.get("sql") == "BEGIN IMMEDIATE":
                # A fresh BEGIN invalidates any prior identity record even if
                # this call is not the receipt context or later raises.
                begins.pop(id(frame.f_locals.get("self")), None)
            if relative == "server/lib/db.py" and receipt and frame.f_locals.get("sql") == "BEGIN IMMEDIATE":
                calls[id(frame)] = append({"kind": "real_BEGIN", "path": relative,
                                          "code_sha256": digest, "outcome": "pending"})
            elif relative == "server/lib/janitor_autonomy.py" and code.co_qualname == "_automatic_hotseat_owns_quota":
                # Observe the helper's OWN original catch/return boundary, not
                # just the resolve/get calls it forwards. The enclosing test's
                # real inside cell and current SQLite connection bind writer
                # placement without calling or replacing any target function.
                owner = next((f for f in chain[1:] if Path(f.f_code.co_filename) == ROOT / TEST
                              and f.f_code.co_name == NODE.split("::")[1]), None)
                inside = owner.f_locals.get("inside") if owner is not None else None
                if type(inside) is not list or len(inside) != 1 or type(inside[0]) is not bool:
                    state["diagnostic_errors"].append("helper_inside_identity_unknown")
                else:
                    local = getattr(sys.modules.get("lib.db"), "_LOCAL", None)
                    conn = getattr(local, "conn", None)
                    begin = begins.get(id(conn)) if conn is not None and conn.in_transaction else None
                    calls[id(frame)] = append({"kind": "ownership_helper", "name": code.co_name,
                        "path": relative, "code_sha256": digest, "under_writer": inside[0],
                        "successful_real_BEGIN_seq": begin, "outcome": "pending"})
            elif relative in ("server/lib/janitor_builtins.py", "server/lib/janitors.py"):
                forwarding = next((f for f in chain[1:] if f.f_code.co_name == "observing"
                                   and Path(f.f_code.co_filename) == ROOT / TEST
                                   and getattr(f.f_locals.get("_original"), "__code__", None) is code), None)
                if forwarding is not None:
                    inside = forwarding.f_locals.get("inside")
                    if type(inside) is not list or len(inside) != 1 or type(inside[0]) is not bool:
                        state["diagnostic_errors"].append("inside_identity_unknown")
                    else:
                        # The contextmanager generator is suspended at yield,
                        # so it is NOT an active ancestor during the body.
                        # Read the existing thread-local connection, without
                        # calling db.conn() or keeping the object after trace.
                        db_module = sys.modules.get("lib.db")
                        local = getattr(db_module, "_LOCAL", None)
                        conn = getattr(local, "conn", None)
                        begin = begins.get(id(conn)) if conn is not None and conn.in_transaction else None
                        calls[id(frame)] = append({"kind": "forwarded_original", "name": code.co_name,
                            "path": relative, "code_sha256": digest, "under_writer": inside[0],
                            "successful_real_BEGIN_seq": begin, "outcome": "pending"})
        elif event == "exception" and id(frame) in calls and calls[id(frame)] is not None:
            calls[id(frame)].setdefault("exception_types", []).append(arg[0].__name__)
        elif event == "return" and id(frame) in calls:
            row = calls.pop(id(frame))
            if row is not None:
                row["outcome"] = "normal_return" if normal_return(frame) else "error_unwind"
                if row["kind"] == "real_BEGIN" and row["outcome"] == "normal_return":
                    conn = frame.f_locals.get("self")
                    row["in_transaction_after_return"] = conn is not None and conn.in_transaction is True
                    if row["in_transaction_after_return"]:
                        begins[id(conn)] = row["seq"]
    except Exception as exc:
        # A failed diagnostic never fabricates a normal original return. Keep
        # the test's actual behavior and make the final witness inconclusive.
        if len(state["diagnostic_errors"]) < 64:
            state["diagnostic_errors"].append(type(exc).__name__)
    return observe


def pytest_sessionstart(session):
    global old_trace, enabled
    if not PINS or sys.version_info[:2] != (3, 12) or sys.flags.optimize != 0:
        raise pytest.UsageError("witness source pins/interpreter mismatch")
    for relative, digest in PINS.items():
        raw = (ROOT / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise pytest.UsageError("witness source mismatch: " + relative)
        if relative in WATCH:
            # Compile to an unexecuted reference code graph with exact filename.
            for code in descend(compile(raw, str(ROOT / relative), "exec", dont_inherit=True, optimize=0)):
                if code.co_qualname in WATCH[relative]:
                    expected[(relative, code.co_qualname)] = fingerprint(code)
    if len(expected) != sum(map(len, WATCH.values())):
        raise pytest.UsageError("witness expected original code incomplete")
    state["runtime"] = {"executable": str(Path(sys.executable).resolve()), "version": sys.version,
                        "optimize": sys.flags.optimize, "source_hashes": PINS}
    old_trace = sys.gettrace()
    if old_trace is not None:
        raise pytest.UsageError("witness would replace an existing tracer")
    sys.settrace(observe)
    enabled = True


def pytest_collection_modifyitems(session, config, items):
    if [i.nodeid for i in items] != [NODE]:
        raise pytest.UsageError("witness requires exactly one unparametrized node")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if item.nodeid != NODE:
        return
    row = {"when": report.when, "outcome": report.outcome, "nodeid": report.nodeid}
    if call.excinfo is not None:
        last = call.excinfo.traceback[-1]
        row.update(exception_type=call.excinfo.type.__name__, failure_line_1based=last.lineno + 1,
                   failure_path=str(last.path), assertion_error=call.excinfo.type is AssertionError)
    state["reports"].append(row)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    global enabled
    # Post-yield runs after all ordinary sessionfinish hooks, including the
    # maintained provider guard. Its final outside list must be explicitly [].
    yield
    if enabled:
        if sys.gettrace() is not observe:
            state["diagnostic_errors"].append("tracer_replaced")
        sys.settrace(old_trace)
        enabled = False
    reports = state["reports"]
    originals = [r for r in state["events"] if r["kind"] == "forwarded_original"]
    under = [r for r in originals if r["under_writer"]]
    helpers = [r for r in state["events"] if r["kind"] == "ownership_helper"]
    helpers_under = [r for r in helpers if r["under_writer"]]
    call = [r for r in reports if r["when"] == "call"]
    guard = getattr(session.config, "_clarp_guard", None)
    state["guard_outside"] = None if guard is None else guard.get("outside")
    state["session_exit"] = int(session.exitstatus)
    state["qualified"] = bool(
        not state["diagnostic_errors"] and not calls and state["line527"] and state["line528"]
        and not state["line529"] and state.get("assert528_exception") is True
        and len(reports) == 3 and [r["when"] for r in reports] == ["setup", "call", "teardown"]
        and reports[0]["outcome"] == reports[2]["outcome"] == "passed"
        and len(call) == 1 and call[0]["outcome"] == "failed" and call[0].get("assertion_error") is True
        and call[0].get("failure_line_1based") == 528 and Path(call[0].get("failure_path", "")) == ROOT / TEST
        and any(r["name"] == "resolve" and r["outcome"] == "normal_return" for r in originals)
        and under and all(r["outcome"] == "normal_return" and not r.get("exception_types")
                          and type(r["successful_real_BEGIN_seq"]) is int for r in under)
        and all(r["outcome"] == "normal_return" and not r.get("exception_types") for r in originals)
        and helpers_under and all(r["outcome"] == "normal_return" and not r.get("exception_types")
                                  and type(r["successful_real_BEGIN_seq"]) is int for r in helpers_under)
        and all(r["outcome"] == "normal_return" and not r.get("exception_types") for r in helpers)
        and {"AutonomyJanitors.run_once", "AutonomyJanitors.quota_once", "_automatic_hotseat_owns_quota",
             "transaction", "_TrackedConnection.execute"} <= {r["qualname"] for r in state["loaded"]}
        and state["guard_outside"] == [] and state["session_exit"] == 1)
    # Preserve only diagnostic metadata; raw pytest and JUnit are separate.
    out = ROOT / "witness-output"
    with (out / "observer.json").open("x") as handle:
        handle.write(json.dumps(state, indent=2) + "\n")
