"""Hotseat switcher: change the default Claude or Codex account before quota runs out.

Hotseat (the local account desk CLI) already knows every saved account's quota.
This Janitor reads that report, compares the account in use with the rest and,
when the account in use drops under a floor while another account still has
room, switches the machine-wide default through Hotseat. No model is involved.

The floors are Janitor options (Claude 5-hour window, Codex weekly window). When
no other account clears the floor the ladder drops to 10 % remaining, then to
0 %, so a switch only happens when it buys real headroom: the account in use
must be under the rung and the target at or above it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Callable

ROLE = "account-hotseat"
LADDER_TAIL = (10, 0)
SWITCH_TIMEOUT = 60


def rungs(floor: int) -> list[int]:
    """The floor first, then the fixed lower rungs that still sit below it."""
    steps = [int(floor)]
    steps += [step for step in LADDER_TAIL if step < steps[0]]
    return steps


def _percent_remaining(used) -> int | None:
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return max(0, min(100, round(100 - used * 100)))


def parse_claude(payload) -> list[dict]:
    """`hotseat list --json`: one entry per saved Claude account, 5-hour first."""
    out = []
    for account in (payload or {}).get("accounts", []) if isinstance(payload, dict) else []:
        if not isinstance(account, dict) or not account.get("alias"):
            continue
        usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
        remaining = None
        if not account.get("error"):
            if usage.get("limited") or usage.get("status") == "rejected":
                remaining = 0
            else:
                remaining = _percent_remaining(usage.get("used_5h"))
        out.append({"alias": str(account["alias"]), "active": bool(account.get("is_active")),
                    "remaining": remaining, "secondary": _percent_remaining(usage.get("used_7d"))})
    return out


def parse_codex(payload) -> list[dict]:
    """`hotseat codex --json`: one entry per saved Codex profile, weekly window."""
    out = []
    for account in (payload or {}).get("accounts", []) if isinstance(payload, dict) else []:
        if not isinstance(account, dict) or not account.get("alias"):
            continue
        usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
        remaining = None
        if not usage.get("error"):
            if usage.get("blocked"):
                remaining = 0
            elif usage.get("usable") is not False:
                remaining = _percent_remaining(usage.get("worst_used"))
        out.append({"alias": str(account["alias"]), "active": bool(account.get("is_active")),
                    "remaining": remaining, "secondary": None})
    return out


@dataclass(frozen=True)
class HotseatProvider:
    """How Hotseat reports and switches one provider; looked up, never branched on."""
    window: str
    read_args: tuple[str, ...]
    read_timeout: int
    parse: Callable[[object], list[dict]]
    floor_key: str
    interval_key: str
    switch_args: Callable[[str], list[str]]
    # `hotseat switch --json` confirms with {"switched": true}; the Codex
    # profile helper only has its exit code.
    confirms_json: bool


PROVIDERS: dict[str, HotseatProvider] = {
    "claude": HotseatProvider("5-hour", ("list", "--json"), 60, parse_claude, "claude_min_remaining",
                              "interval_seconds", lambda alias: ["switch", alias, "--yes", "--json"], True),
    "codex": HotseatProvider("weekly", ("codex", "--json"), 150, parse_codex, "codex_min_remaining",
                             "codex_interval_seconds", lambda alias: ["codex-account", "switch", alias], False),
}
WINDOW_LABEL = {provider: spec.window for provider, spec in PROVIDERS.items()}


def decide(accounts: list[dict], floor: int) -> dict:
    """Walk the ladder: switch only when the account in use is under a rung and
    another account is at or above it. Unknown readings never count as room."""
    active = next((a for a in accounts if a["active"]), None)
    if active is None:
        return {"action": "skip", "reason": "Hotseat reports no account in use"}
    have = active["remaining"]
    if have is None:
        return {"action": "skip", "reason": f"{active['alias']}: quota unknown"}
    for rung in rungs(floor):
        beneath = have < rung if rung > 0 else have <= 0
        if not beneath:
            return {"action": "noop", "active": active["alias"], "remaining": have, "rung": rung}
        candidates = [a for a in accounts if not a["active"] and a["remaining"] is not None
                      and a["remaining"] > have and (a["remaining"] >= rung if rung > 0 else a["remaining"] > 0)]
        if candidates:
            best = max(candidates, key=lambda a: (a["remaining"], a["secondary"] or 0, a["alias"]))
            return {"action": "switch", "active": active["alias"], "remaining": have,
                    "target": best["alias"], "target_remaining": best["remaining"], "rung": rung}
    return {"action": "exhausted", "active": active["alias"], "remaining": have}


def _run(command: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)


def read_accounts(command: str, provider: str) -> list[dict]:
    """Ask Hotseat for every saved account of one provider. Raises on any failure."""
    spec = PROVIDERS[provider]
    proc = _run(command, list(spec.read_args), spec.read_timeout)
    if proc.returncode:
        raise RuntimeError(f"hotseat exited {proc.returncode}")
    return spec.parse(json.loads(proc.stdout))


def switch_account(command: str, provider: str, alias: str) -> bool:
    """Make `alias` the machine-wide default through Hotseat. True only on a confirmed switch."""
    spec = PROVIDERS[provider]
    proc = _run(command, spec.switch_args(alias), SWITCH_TIMEOUT)
    if proc.returncode:
        return False
    if not spec.confirms_json:
        return True
    try:
        return bool(json.loads(proc.stdout).get("switched"))
    except (ValueError, AttributeError):
        return False


def floor_for(options: dict, provider: str) -> int:
    return int(options[PROVIDERS[provider].floor_key])


def interval_for(options: dict, provider: str) -> int:
    return int(options[PROVIDERS[provider].interval_key])


def preview(provider: str, decision: dict, *, mode: str, switched: bool | None = None) -> str:
    """One line for the phone; no credentials, no command output."""
    name = provider.capitalize()
    window = WINDOW_LABEL[provider]
    if decision["action"] == "switch":
        detail = (f"{decision['target']} has {decision['target_remaining']}% of the {window} window left; "
                  f"{decision['active']} had {decision['remaining']}%")
        if mode != "automatic":
            return f"{name}: would switch to {decision['target']}. {detail}"
        if switched:
            return f"{name}: switched to {decision['target']}. {detail}"
        return f"{name}: could not switch to {decision['target']}. {detail}"
    if decision["action"] == "exhausted":
        return f"{name}: {decision['active']} has {decision['remaining']}% of the {window} window left and no saved account has more"
    return ""
