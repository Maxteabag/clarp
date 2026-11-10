"""Exact one-node witness using unchanged e26 tests.yml containment contract.

Source only until exact B review and separate lead publication/run authority.
No privileged creation/drop code, shared helper edits, or new reaper framework.
Ephemeral hosted VM teardown supplies closure; no heavy DRAIN claim.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

SOURCE = "bd2fd2a9d9722332fcbf5d5b2c54538467c3b39c"
BRANCH = "refs/heads/ci/backlog48-witness-bd2fd-observe-r2"
NODE = "tests/unit/test_janitor_hotseat.py::test_receipt_writer_never_builds_full_janitor_views"
WRAPPER_SHA = "07f228044e915297a84a52201e68e40a34809928fcf0ecb8d300b692ee5d067f"
SELFCHECK_SHA = "91a5b503b559eb6e7f1ceaf495199496c65851627e361cffdf47dc4be60546fb"


def main():
    work = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    source, controller = work / "source", work / "controller"
    out = work / "witness-evidence"
    out.mkdir(mode=0o700)
    summary = {"result": "INCONCLUSIVE", "source": SOURCE, "node": NODE,
               "phases": [], "actual_pytest_exit": None, "qualified_RED": False,
               "closure": "ephemeral_Actions_VM_teardown; heavy_DRAIN_NA"}
    rc = 1
    start = None
    output = None
    journal_count = 0
    retention_attempted = False
    summary["phase_journal_errors"] = []
    summary["partial_retention"] = {}
    summary["partial_retention_errors"] = []
    def left(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("absolute_budget_exhausted")
        return remaining
    def git(root, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(root), *args],
            timeout=min(5, left(start + 30))).decode().strip()
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    def journal(phase):
        nonlocal journal_count
        if output is None:
            return
        fd = None
        try:
            journal_count += 1
            if journal_count > 32:
                raise ValueError("phase_count_limit")
            record = (json.dumps({"phase": phase, "monotonic_ns": time.monotonic_ns()}) + "\n").encode()
            fd = os.open(output / "phase-journal.jsonl",
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            if os.fstat(fd).st_size + len(record) > 8192:
                raise ValueError("phase_byte_limit")
            if os.write(fd, record) != len(record):
                raise OSError("short_phase_write")
            os.fsync(fd)
        except Exception as exc:
            if len(summary["phase_journal_errors"]) < 16:
                summary["phase_journal_errors"].append(type(exc).__name__)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception as exc:
                    if len(summary["phase_journal_errors"]) < 16:
                        summary["phase_journal_errors"].append(type(exc).__name__)
    def preserve_partials():
        nonlocal retention_attempted
        if retention_attempted or output is None:
            return
        retention_attempted = True  # one attempt, never recopy or repair a partial
        if start is None or time.monotonic() >= start + 150:
            summary["partial_retention_errors"].append("receipt_budget_exhausted_no_optional_io")
            return
        journal("controller_retention_enter")
        # Fixed allowlist only. The journal is last so its copied prefix also
        # includes the retention-entry marker. These bytes are diagnostics,
        # never an alternative expected-RED/closure or producer-success proof.
        for name, limit in (("observer.json", 10 * 1024 * 1024),
                            ("node.xml", 10 * 1024 * 1024), ("phase-journal.jsonl", 8192)):
            fd = None
            row = {"status": "pending", "snapshot": "diagnostic_only_may_be_partial"}
            summary["partial_retention"][name] = row
            try:
                left(start + 150)
                fd = os.open(output / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > limit:
                    raise RuntimeError("not_owned_regular_bounded_artifact")
                total = 0
                with (out / name).open("xb") as target:
                    while True:
                        left(start + 150)
                        chunk = os.read(fd, min(65536, limit + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise RuntimeError("partial_artifact_grew_over_limit")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                row.update(status="retained", bytes=total)
            except FileNotFoundError:
                row["status"] = "missing"
            except Exception as exc:
                row.update(status="error", error_type=type(exc).__name__)
                summary["partial_retention_errors"].append(name + ":" + type(exc).__name__)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except Exception as exc:
                        row.update(status="error", close_error_type=type(exc).__name__)
                        summary["partial_retention_errors"].append(name + ":close:" + type(exc).__name__)
    try:
        if (os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REF")) != ("Maxteabag/clarp", "push", BRANCH):
            raise RuntimeError("unexpected_branch_context")
        clock = json.loads((work / "witness-clock.json").read_text())
        if (set(clock) != {"monotonic_ns", "boot_id"} or type(clock["monotonic_ns"]) is not int
                or clock["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            raise RuntimeError("invalid_initial_clock")
        start = clock["monotonic_ns"] / 1e9
        if not 0 <= time.monotonic() - start < 30:
            raise RuntimeError("checkout_tool_setup_exhausted_30s_setup")
        summary["whole_deadline_monotonic"] = start + 180
        # SAME initial clock, never renewed at controller entry. The step cap
        # is3min too, so it cannot cut the declared60s receipt reserve to120s.
        # Split that reserve into30s local receipts +30s upload/final status;
        # the job hard3min and final post-upload clock check remain authoritative.
        receipt_deadline = start + 150
        summary["receipt_deadline_monotonic"] = receipt_deadline
        summary["reserved_receipt_upload_status_s"] = 60
        summary["reserved_upload_final_status_s"] = 30
        if git(source, "rev-parse", "HEAD") != SOURCE or git(controller, "rev-parse", "HEAD") != os.environ["GITHUB_SHA"]:
            raise RuntimeError("checkout_identity_mismatch")
        if git(source, "status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("modified_source")
        if digest(source / "scripts/ci_contained.sh") != WRAPPER_SHA or digest(source / "scripts/ci_containment_selfcheck.py") != SELFCHECK_SHA:
            raise RuntimeError("maintained_containment_identity_mismatch")
        output = source / "witness-output"
        output.mkdir(mode=0o700)
        journal("controller_setup_enter")
        plugin_source = controller / "tools/backlog48/witness_observer.py"
        plugin = source / "witness_observer.py"
        with plugin.open("xb") as handle:
            handle.write(plugin_source.read_bytes())
        summary["observer_sha256"] = digest(plugin)
        summary["controller_hashes"] = {p: digest(controller / p) for p in (
            "tools/backlog48/witness_observer.py", "tools/backlog48/witness_runner.py",
            ".github/workflows/backlog48-witness.yml")}
        (out / "source-tree.txt").write_text(git(source, "ls-tree", "-r", "HEAD") + "\n")
        # Only dependencies needed by the maintained Python node; no npm/FFmpeg
        # build or extra tests. Checkout/actions/interpreter/deps ALL count in30.
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("missing_maintained_uv")
        with (out / "setup.stdout").open("wb") as stdout, (out / "setup.stderr").open("wb") as stderr:
            p = subprocess.run([uv, "sync", "--frozen", "--group", "dev", "--python", "3.12"],
                cwd=source, stdout=stdout, stderr=stderr, timeout=left(start + 30))
        summary["phases"].append({"name": "setup", "actual_exit": p.returncode, "elapsed_from_start_s": time.monotonic() - start})
        journal("controller_setup_returned")
        if p.returncode != 0:
            raise RuntimeError("dependency_setup_failed")
        left(start + 30)
        journal("controller_command_enter")
        command_start = time.monotonic()
        deadline = min(command_start + 90, start + 180 - 60)
        # TERM10 + finaljoin10 are reserved INSIDE the 90s command allowance.
        runtime = left(deadline) - 20
        if runtime <= 60:
            raise RuntimeError("insufficient_budget_for_existing_60s_test_timeout")
        argv = ["/usr/bin/timeout", "--signal=TERM", "--kill-after=10s", str(runtime) + "s",
                "bash", "scripts/ci_contained.sh", ".venv/bin/python", "-u", "-B", "-m", "pytest",
                "-n", "0", "--timeout=60", "-p", "witness_observer", NODE,
                "--junitxml=witness-output/node.xml", "--tb=long"]
        summary["command"] = argv
        with (out / "pytest.stdout").open("wb") as stdout, (out / "pytest.stderr").open("wb") as stderr:
            proc = subprocess.Popen(argv, cwd=source, stdout=stdout, stderr=stderr)
            summary["owned_command_pid"] = proc.pid
            try:
                status = proc.wait(timeout=left(deadline))
            except subprocess.TimeoutExpired:
                # No retry, late numeric signal, shared cleanup or host action.
                # Job step and VM teardown are the named outer contract.
                summary["command_terminal"] = "UNKNOWN_outer_step_VM_teardown"
                raise RuntimeError("command_join_budget_exhausted")
        summary["actual_pytest_exit"] = status  # raw wrapper/timeout exit, never collector0
        summary["phases"].append({"name": "one_node", "actual_exit": status,
                                  "elapsed_s": time.monotonic() - command_start})
        journal("controller_command_returned")
        left(deadline)
        preserve_partials()  # includes raw137 and ordinary expected raw1 alike
        if status != 1:
            raise RuntimeError("not_ordinary_expected_pytest_failure1")
        if summary["partial_retention_errors"] or summary["phase_journal_errors"]:
            raise RuntimeError("partial_evidence_io_failed")
        if digest(plugin) != summary["observer_sha256"]:
            raise RuntimeError("observer_changed")
        # Local receipts use the first30 of the SAME60s post-command reserve;
        # retain the last30 for upload and the final status/clock check.
        left(receipt_deadline)
        for name in ("observer.json", "node.xml"):
            src = out / name
            if not src.is_file() or src.stat().st_size > 10 * 1024 * 1024:
                raise RuntimeError("missing_or_oversized_" + name)
        if not (out / "phase-journal.jsonl").is_file():
            raise RuntimeError("missing_phase_journal")
        observation = json.loads((out / "observer.json").read_text())
        tree = ET.parse(out / "node.xml")
        cases = tree.findall(".//testcase")
        if (len(cases) != 1 or cases[0].get("name") != NODE.split("::")[1]
                or len(tree.findall(".//failure")) != 1 or tree.findall(".//error") or tree.findall(".//skipped")):
            raise RuntimeError("not_exact_one_failed_node_JUnit")
        # Recheck tracked source under the whole receipt reserve, not setup30.
        raw = subprocess.check_output(["/usr/bin/git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
                                      timeout=min(5, left(receipt_deadline)))
        if raw.strip() or observation.get("qualified") is not True or observation.get("node") != NODE:
            raise RuntimeError("observer_or_source_did_not_qualify_528_after_527")
        summary.update(result="EXPECTED_RED_528_AFTER_527", qualified_RED=True)
        rc = 0
    except BaseException as exc:
        summary.update(error_type=type(exc).__name__, exception_traceback=traceback.format_exc())
        traceback.print_exc()
    finally:
        # Retain allowed existing partials for setup/error/timeout paths too,
        # without masking the primary or renewing the original receipt bound.
        preserve_partials()
        summary["elapsed_from_initial_clock_s"] = None if start is None else time.monotonic() - start
        if start is None or time.monotonic() >= start + 150:
            summary.update(result="INCONCLUSIVE", qualified_RED=False, budget="UNKNOWN_OR_LATE")
            rc = 1
        (out / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
        index = {p.name: digest(p) for p in sorted(out.iterdir()) if p.is_file()}
        (out / "SHA256-INDEX.json").write_text(json.dumps(index, indent=2) + "\n")
        if start is not None and time.monotonic() >= start + 150:
            # A late publication cannot leave an accepted result behind.
            summary.update(result="INCONCLUSIVE", qualified_RED=False, budget="LATE_RECEIPT")
            (out / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
            index["result.json"] = digest(out / "result.json")
            (out / "SHA256-INDEX.json").write_text(json.dumps(index, indent=2) + "\n")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
