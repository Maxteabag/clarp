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

from dataclasses import dataclass, field
import json
import re
import subprocess
from typing import Callable

from . import hotseat_desk_reading, hotseat_status

ROLE = "account-hotseat"
LADDER_TAIL = (10, 0)
SWITCH_TIMEOUT = 60


# Private optional-repair preparation. These values never enter the public
# Hotseat snapshot or an audit payload; the runner will persist only a HMAC.
REPAIR_MAX_PROFILES = 32
_REPAIR_ALIAS = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_REPAIR_ORG = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


@dataclass(frozen=True)
class RepairProfile:
    alias: str = field(repr=False)
    email: str = field(repr=False)
    org_uuid: str = field(repr=False)


def _repair_rows(payload) -> list[dict]:
    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > REPAIR_MAX_PROFILES:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _saved_repair_profiles(payload) -> tuple[RepairProfile, ...]:
    """Exact inactive saved identity; no text, quota or live-identity inference.

    The primitive independently reopens the exact saved profile and rejects
    unsupported roots, active defaults or newer credentials before publishing.
    This parser grants none of those effect permissions or quota usability.
    """
    rows = _repair_rows(payload)
    profiles = []
    for row in rows:
        alias, email, org = row.get("alias"), row.get("email"), row.get("org_uuid")
        if (row.get("is_active") is not False
                or not isinstance(alias, str) or not _REPAIR_ALIAS.fullmatch(alias)
                or sum(other.get("alias") == alias for other in rows) != 1
                or not isinstance(email, str) or not 3 <= len(email) <= 320
                or email != email.strip() or "@" not in email
                or not isinstance(org, str) or not _REPAIR_ORG.fullmatch(org)
                or org.replace("-", "") == "0" * 32):
            continue
        profiles.append(RepairProfile(alias, email, org))
    return tuple(profiles)


def repair_profiles(payload) -> tuple[RepairProfile, ...]:
    """Only a positively typed current rejection admits an exact saved profile."""
    # The reviewed emitter returns no quota Summary on a typed failure. A row
    # simultaneously claiming a quota result/rejection is not that trusted
    # schema; do not turn exhausted quota (or malformed rows) into relogin work.
    rejected = {row.get("alias") for row in _repair_rows(payload)
                if row.get("error_code") == "auth_error" and isinstance(row.get("alias"), str)
                and isinstance(row.get("error"), str) and bool(row["error"])
                and "usage" in row and row["usage"] is None}
    return tuple(profile for profile in _saved_repair_profiles(payload) if profile.alias in rejected)


def _repair_headroom(value) -> bool:
    # The range test also rejects NaN/infinity without converting huge integers.
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value < 1)


def repair_profile_usable(payload, profile: RepairProfile, *, requested_model: str,
                          not_before_ms: int, now_ms: int) -> bool:
    """Fresh exact identity AND actual reported quota for the requested model.

    A new read with no per-model report (including a header-probe fallback) is
    still unverified. Callers must supply the canonical requested scope label;
    this function never guesses an alias/model from an error or another account.
    It neither marks state repaired nor switches any account.
    """
    if (type(not_before_ms) is not int or type(now_ms) is not int
            or not 0 <= now_ms - not_before_ms <= _auth_repair_provider().read_timeout * 1000
            or not isinstance(requested_model, str) or not 1 <= len(requested_model) <= 160
            or requested_model != requested_model.strip()):
        return False
    stamp = payload.get("generated_at") if isinstance(payload, dict) else None
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not not_before_ms - 1000 <= stamp * 1000 <= now_ms + 1000):
        return False
    rows = [row for row in _repair_rows(payload) if row.get("alias") == profile.alias]
    if len(rows) != 1:
        return False
    row = rows[0]
    if (row.get("is_active") is not False or not isinstance(row.get("email"), str)
            or row["email"].casefold() != profile.email.casefold() or row.get("org_uuid") != profile.org_uuid
            or row.get("error") or "error_code" not in row or row["error_code"] is not None):
        return False
    usage = row.get("usage")
    if (not isinstance(usage, dict) or usage.get("status") != "allowed"
            or usage.get("available") is not True or usage.get("limited") is not False
            or usage.get("degraded") or usage.get("fallback")
            or not _repair_headroom(usage.get("used_5h")) or not _repair_headroom(usage.get("used_7d"))):
        return False
    blocked, scoped = usage.get("blocked_models"), usage.get("scoped")
    if (not isinstance(blocked, list) or len(blocked) > REPAIR_MAX_PROFILES
            or not all(isinstance(model, str) for model in blocked)
            or requested_model.casefold() in {model.casefold() for model in blocked}
            or not isinstance(scoped, list) or len(scoped) > REPAIR_MAX_PROFILES):
        return False
    limits = [limit for limit in scoped if isinstance(limit, dict)
              and isinstance(limit.get("model"), str)
              and limit["model"].casefold() == requested_model.casefold()]
    return (len(limits) == 1 and limits[0].get("exhausted") is False
            and _repair_headroom(limits[0].get("used")))


def rungs(floor: int) -> list[int]:
    """The floor first, then the fixed lower rungs that still sit below it."""
    steps = [int(floor)]
    steps += [step for step in LADDER_TAIL if step < steps[0]]
    return steps


def _percent_remaining(used) -> int | None:
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return max(0, min(100, round(100 - used * 100)))


_CLAUDE_FAMILY_LABELS = (
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
)
_SAFE_MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,39}\Z")


def _claude_model_spec(value) -> tuple[str, str] | None:
    """Normalize a model identifier or scoped-limit label to (family, display_label)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.casefold()
    for family, label in _CLAUDE_FAMILY_LABELS:
        if family in lowered:
            return family, label
    if _SAFE_MODEL_TOKEN.fullmatch(text):
        return lowered, text
    return None


def _unique_model_specs(models) -> tuple[tuple[str, str], ...]:
    seen = set()
    out = []
    for item in models or ():
        spec = _claude_model_spec(item)
        if spec is not None and spec[0] not in seen:
            seen.add(spec[0])
            out.append(spec)
    return tuple(out)


def _parsed_blocked_families(usage: dict) -> tuple[str, ...]:
    blocked = []
    seen = set()
    raw_blocked = usage.get("blocked_models")
    if isinstance(raw_blocked, list):
        for item in raw_blocked[:REPAIR_MAX_PROFILES]:
            spec = _claude_model_spec(item)
            if spec is not None and spec[0] not in seen:
                seen.add(spec[0])
                blocked.append(spec[0])
    raw_scoped = usage.get("scoped")
    if isinstance(raw_scoped, list):
        for limit in raw_scoped[:REPAIR_MAX_PROFILES]:
            if not isinstance(limit, dict):
                continue
            spec = _claude_model_spec(limit.get("model"))
            if spec is None or spec[0] in seen:
                continue
            rem = _percent_remaining(limit.get("used"))
            if limit.get("exhausted") is True or rem == 0:
                seen.add(spec[0])
                blocked.append(spec[0])
    return tuple(blocked)


def _parsed_scoped_remaining(usage: dict) -> dict[str, int]:
    scoped = {}
    raw_scoped = usage.get("scoped")
    if not isinstance(raw_scoped, list):
        return scoped
    for limit in raw_scoped[:REPAIR_MAX_PROFILES]:
        if not isinstance(limit, dict):
            continue
        spec = _claude_model_spec(limit.get("model"))
        if spec is None:
            continue
        rem = 0 if limit.get("exhausted") is True else _percent_remaining(limit.get("used"))
        if rem is not None:
            prev = scoped.get(spec[0])
            scoped[spec[0]] = rem if prev is None else min(prev, rem)
    return scoped


def parse_claude(payload) -> list[dict]:
    """`hotseat list --json`: one entry per saved Claude account, 5-hour + weekly + model limits."""
    out = []
    for account in (payload or {}).get("accounts", []) if isinstance(payload, dict) else []:
        if not isinstance(account, dict) or not account.get("alias"):
            continue
        usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
        remaining = None
        entry = {"alias": str(account["alias"]), "active": bool(account.get("is_active")),
                 "remaining": None, "secondary": _percent_remaining(usage.get("used_7d"))}
        if not account.get("error"):
            overage_ok = (bool(usage.get("extra_usage_enabled"))
                          or usage.get("overage_status") in {"allowed", "allowed_warning"})
            used_7d = usage.get("used_7d")
            weekly_exhausted = (
                not overage_ok
                and (usage.get("status_7d") == "rejected"
                     or entry["secondary"] == 0
                     or (not isinstance(used_7d, bool)
                         and isinstance(used_7d, (int, float))
                         and used_7d >= 1.0))
            )
            if (usage.get("limited")
                    or usage.get("status") == "rejected"
                    or usage.get("available") is False
                    or weekly_exhausted):
                remaining = 0
            else:
                remaining = _percent_remaining(usage.get("used_5h"))
            entry["remaining"] = remaining
            blocked = _parsed_blocked_families(usage)
            scoped = _parsed_scoped_remaining(usage)
            if blocked:
                entry["blocked_models"] = blocked
            if scoped:
                entry["scoped_remaining"] = scoped
            if usage.get("degraded"):
                entry["degraded"] = True
            if overage_ok:
                entry["overage"] = True
        out.append(entry)
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


def _repair_model_pin(value: str | None) -> str:
    # Keep the maintained normalization lazy, as before, to avoid importing
    # capability discovery while the Hotseat module is initialized.
    from .provider_capabilities import claude_model_pin
    return claude_model_pin(value)


@dataclass(frozen=True)
class HotseatAuthRepair:
    """A declared repair strategy, including its model and identity domain."""
    identity_provider: str
    model_pin: Callable[[str | None], str]

    def identity_key(self, profile: RepairProfile) -> str:
        # First admission retains the canonical identity owner's salt creation.
        return hotseat_status.identity_key(self.identity_provider, profile.alias,
            email=profile.email.casefold(), org=profile.org_uuid, account_id="auth-repair")

    def bound_key(self, salt: str, alias: str, *, email="", org="", alias_only=False) -> str:
        # The same exact persisted protocol, owned by the registered strategy.
        # OFF callers already possess a validated salt: never call its creator.
        import hashlib
        import hmac
        message = "\0".join(("identity", self.identity_provider, alias, email.casefold(), org,
                               "auth-repair-alias" if alias_only else "auth-repair"))
        return hmac.new(salt.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]

    def requested_model(self) -> str:
        from . import backends, config
        backend = backends.by_id(self.identity_provider)
        model, _ = backend.default_model_effort(config.load())
        return self.model_pin(model) or self.model_pin(backend.cli_default_model())


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
    auth_repair: HotseatAuthRepair | None = None
    model_scoped: bool = False
    # Flags that go after the read's subcommand when only "which account is in
    # use" is asked (the Hotseat desk, before a switch): they leave out what
    # that question does not need. Codex's quota probe is the slow part.
    active_read_flags: tuple[str, ...] = ()
    # How the Hotseat desk reads this provider's windows and rows.
    desk: hotseat_desk_reading.DeskReading | None = None


PROVIDERS: dict[str, HotseatProvider] = {
    "claude": HotseatProvider("5-hour", ("list", "--json"), 60, parse_claude, "claude_min_remaining",
                              "interval_seconds", lambda alias: ["switch", alias, "--yes", "--json"], True,
                              auth_repair=HotseatAuthRepair("claude", _repair_model_pin), model_scoped=True,
                              desk=hotseat_desk_reading.CLAUDE_READING),
    "codex": HotseatProvider("weekly", ("codex", "--json"), 150, parse_codex, "codex_min_remaining",
                             "codex_interval_seconds", lambda alias: ["codex-account", "switch", alias], False,
                             active_read_flags=("--no-quota",), desk=hotseat_desk_reading.CODEX_READING),
}
WINDOW_LABEL = {provider: spec.window for provider, spec in PROVIDERS.items()}


def _auth_repair_provider() -> HotseatProvider:
    """Private repair state belongs to the single explicitly registered policy.

    The existing ledger is not a generic multi-provider protocol. Refuse an
    absent/ambiguous registration rather than borrowing another identity domain.
    """
    supported = [spec for spec in PROVIDERS.values() if spec.auth_repair is not None]
    if len(supported) != 1:
        raise RepairPolicyError("repair_policy_unavailable")
    return supported[0]


def _account_model_remaining(account: dict, family: str) -> int | None:
    """Effective headroom % for `family` on `account`, or None when per-model quota is unknown."""
    general = account.get("remaining")
    if general is None:
        return None
    if general <= 0:
        return 0
    if family in (account.get("blocked_models") or ()):
        return 0
    if account.get("degraded"):
        return None
    scoped = (account.get("scoped_remaining") or {}).get(family)
    if scoped is not None:
        return min(general, scoped)
    return general


DEFAULT_SWITCH_COOLDOWN_MS = 15 * 60 * 1000


def _leximin_headroom(
    account: dict, req_specs: tuple[tuple[str, str], ...],
) -> tuple[int, ...]:
    """Sorted ascending vector of (general_remaining, *required_model_remaining).

    Requiring `_leximin_headroom(cand) > _leximin_headroom(active)` whenever
    `req_specs` is non-empty establishes a strict partial order over finite
    quota states, preventing 2-way or k-way switch cycles across ticks.
    """
    general = int(account.get("remaining") or 0)
    if not req_specs:
        return (general,)
    values = [general]
    for fam, _ in req_specs:
        rem = _account_model_remaining(account, fam)
        values.append(0 if rem is None else int(rem))
    return tuple(sorted(values))


def _reversal_blocked(
    active: dict,
    cand: dict,
    *,
    floor: int,
    req_specs: tuple[tuple[str, str], ...],
    last_switch: dict | None,
    now_ms: int | None,
    cooldown_ms: int,
) -> bool:
    """Guard against flipping straight back to `last_switch['from']` within
    `cooldown_ms` unless `active` hit hard 0% exhaustion or `cand` now clears
    `floor` on general and all required model windows."""
    if not isinstance(last_switch, dict) or now_ms is None or cooldown_ms <= 0:
        return False
    if last_switch.get("to") != active.get("alias") or last_switch.get("from") != cand.get("alias"):
        return False
    at = last_switch.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return False
    if not (0 <= int(now_ms) - int(at) < int(cooldown_ms)):
        return False
    have = int(active.get("remaining") or 0)
    if have <= 0:
        return False
    if any(
        (_account_model_remaining(active, fam) or 0) <= 0
        and (_account_model_remaining(cand, fam) or 0) > 0
        for fam, _ in req_specs
    ):
        return False
    if int(cand.get("remaining") or 0) < floor:
        return True
    return not all((_account_model_remaining(cand, fam) or 0) >= floor for fam, _ in req_specs)


def decide(accounts: list[dict], floor: int, *,
           required_models: tuple[str, ...] | list[str] = (),
           active_models: tuple[str, ...] | list[str] = (),
           last_switch: dict | None = None,
           now_ms: int | None = None,
           cooldown_ms: int = DEFAULT_SWITCH_COOLDOWN_MS) -> dict:
    """Walk the ladder: switch when the account in use is under a rung on either
    general or required per-model quota and another account clears that rung on
    both general and required model windows without stranding running or
    unblocked required models. Unknown readings never count as room."""
    active = next((a for a in accounts if a["active"]), None)
    if active is None:
        return {"action": "skip", "reason": "Hotseat reports no account in use"}
    have = active["remaining"]
    if have is None:
        return {"action": "skip", "reason": f"{active['alias']}: quota unknown"}
    req_specs = _unique_model_specs(required_models)
    act_specs = _unique_model_specs(active_models)
    running_unblocked = [
        (fam, label) for fam, label in act_specs
        if (_account_model_remaining(active, fam) or 0) > 0
    ]
    unblocked_required = [
        (fam, label) for fam, label in req_specs
        if (_account_model_remaining(active, fam) or 0) > 0
    ]
    active_leximin = _leximin_headroom(active, req_specs)

    def _admissible(cand: dict) -> bool:
        if cand["active"] or cand["remaining"] is None or cand["remaining"] <= 0:
            return False
        if cand.get("secondary") is not None and cand["secondary"] <= 0 and not cand.get("overage"):
            return False
        if not all((_account_model_remaining(cand, afam) or 0) > 0 for afam, _ in running_unblocked):
            return False
        if not all((_account_model_remaining(cand, rfam) or 0) > 0 for rfam, _ in unblocked_required):
            return False
        if req_specs and _leximin_headroom(cand, req_specs) <= active_leximin:
            return False
        if _reversal_blocked(
            active, cand, floor=floor, req_specs=req_specs,
            last_switch=last_switch, now_ms=now_ms, cooldown_ms=cooldown_ms,
        ):
            return False
        return True

    noop_rung = None
    for rung in rungs(floor):
        beneath = have < rung if rung > 0 else have <= 0
        if not beneath:
            noop_rung = rung
            break
        candidates = [
            a for a in accounts
            if _admissible(a)
            and (a["remaining"] >= rung if rung > 0 else a["remaining"] > 0)
        ]
        if candidates:
            if req_specs:
                def _general_rank(cand: dict):
                    vals = [_account_model_remaining(cand, fam) for fam, _ in req_specs]
                    serves_all = int(all(v is not None and (v >= rung if rung > 0 else v > 0) for v in vals))
                    serves_any = sum(1 for v in vals if (v or 0) > 0)
                    min_req = min((v if v is not None else -1) for v in vals)
                    return (serves_all, serves_any, _leximin_headroom(cand, req_specs), min_req,
                            cand["remaining"], cand["secondary"] or 0, cand["alias"])
                best = max(candidates, key=_general_rank)
            else:
                best = max(candidates, key=lambda a: (a["remaining"], a["secondary"] or 0, a["alias"]))
            return {"action": "switch", "active": active["alias"], "remaining": have,
                    "target": best["alias"], "target_remaining": best["remaining"], "rung": rung}
    if noop_rung is None:
        return {"action": "exhausted", "active": active["alias"], "remaining": have}

    for fam, label in req_specs:
        model_have = _account_model_remaining(active, fam)
        if model_have is None:
            continue
        model_cleared = False
        for rung in rungs(floor):
            beneath = model_have < rung if rung > 0 else model_have <= 0
            if not beneath:
                model_cleared = True
                break
            candidates = []
            for cand in accounts:
                if not _admissible(cand):
                    continue
                if not (cand["remaining"] >= rung if rung > 0 else cand["remaining"] > 0):
                    continue
                cand_model = _account_model_remaining(cand, fam)
                if cand_model is None or not (cand_model >= rung if rung > 0 else cand_model > 0):
                    continue
                candidates.append(cand)
            if candidates:
                best = max(
                    candidates,
                    key=lambda a: (_leximin_headroom(a, req_specs),
                                   _account_model_remaining(a, fam) or 0,
                                   a["remaining"], a["secondary"] or 0, a["alias"]),
                )
                return {
                    "action": "switch",
                    "active": active["alias"],
                    "remaining": model_have,
                    "target": best["alias"],
                    "target_remaining": _account_model_remaining(best, fam),
                    "target_general_remaining": best["remaining"],
                    "rung": rung,
                    "model": label,
                }
        if not model_cleared and model_have <= 0:
            if any(afam != fam for afam, _ in running_unblocked):
                return {
                    "action": "noop",
                    "active": active["alias"],
                    "remaining": have,
                    "rung": noop_rung,
                    "blocked_model": label,
                    "model_remaining": 0,
                }
            return {
                "action": "exhausted",
                "active": active["alias"],
                "remaining": 0,
                "general_remaining": have,
                "model": label,
            }

    return {"action": "noop", "active": active["alias"], "remaining": have, "rung": noop_rung}


def _run(command: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)


def read_accounts(command: str, provider: str) -> list[dict]:
    """Ask Hotseat for every saved account of one provider. Raises on any failure."""
    spec = PROVIDERS[provider]
    proc = _run(command, list(spec.read_args), spec.read_timeout)
    if proc.returncode:
        raise RuntimeError(f"hotseat exited {proc.returncode}")
    payload = json.loads(proc.stdout)
    # The status view keeps an allowlisted copy of this same read; no extra call.
    # Never raises: the timestamp, persistence and diagnostics all sit inside its own guard.
    hotseat_status.record_read(provider, payload)
    return spec.parse(payload)


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
    model = decision.get("model")
    if decision["action"] == "switch":
        if model:
            gen = decision.get("target_general_remaining", decision["target_remaining"])
            detail = (f"{decision['target']} has {decision['target_remaining']}% {model} quota "
                      f"({gen}% of the {window} window) left; "
                      f"{decision['active']} had {decision['remaining']}% {model}")
            target_desc = f"{decision['target']} for {model}"
        else:
            detail = (f"{decision['target']} has {decision['target_remaining']}% of the {window} window left; "
                      f"{decision['active']} had {decision['remaining']}%")
            target_desc = str(decision["target"])
        if mode != "automatic":
            return f"{name}: would switch to {target_desc}. {detail}"
        if switched:
            return f"{name}: switched to {target_desc}. {detail}"
        return f"{name}: could not switch to {target_desc}. {detail}"
    if decision["action"] == "exhausted":
        if model:
            gen = decision.get("general_remaining")
            tail = (f" ({decision['active']} keeps {gen}% of the {window} window for other models)"
                    if gen else "")
            return (f"{name}: {decision['active']} has {decision['remaining']}% {model} quota left "
                    f"and no saved account can serve {model}{tail}")
        return f"{name}: {decision['active']} has {decision['remaining']}% of the {window} window left and no saved account has more"
    return ""


def run_summary(provider: str, decision: dict, *, text: str = "") -> str:
    """Summary recorded on the Janitor run and schedule lane."""
    if text:
        return text
    window = WINDOW_LABEL[provider]
    blocked_model = decision.get("blocked_model")
    if blocked_model:
        active = decision.get("active", "?")
        rem = decision.get("remaining", "?")
        model_rem = decision.get("model_remaining", 0)
        return (f"{provider}: {active} has {model_rem}% {blocked_model} quota left and no saved account "
                f"can serve {blocked_model}; keeping {active} ({rem}% of the {window} window) for running models")
    return f"{provider}: {decision.get('active', '?')} keeps {decision.get('remaining', '?')}% of the {window} window"


# Internal settings only. Older releases ignore this key; no schema or public
# Hotseat payload change. The ledger does not execute a repair or switch.
REPAIR_STATE_KEY = "hotseat-auth-repair.state.v1"
_REPAIR_DAY_MS = 86_400_000
_REPAIR_HOUR_MS = 3_600_000
_REPAIR_CODES = frozenset({"started", "repaired", "already_ok", "needs_user", "identity_mismatch",
                           "transient_failure", "cooldown", "interrupted", "verified"})
_REPAIR_STATUSES = frozenset({"repairing", "repaired_unverified", "repaired", "needs_user", "cooldown", "failed"})
_REPAIR_PHASES = frozenset({"reserved", "spawn_pending", "running", "cleaned", "uncertain"})


class RepairPolicyError(Exception):
    """Closed policy failure; callers log only this code, never storage contents."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RepairClaim:
    key: str
    nonce: str = field(repr=False)
    owner_id: str = field(repr=False)
    generation: int
    run_id: str = field(repr=False)
    stop_generation: int


def _repair_process_identity() -> dict:
    import os
    from pathlib import Path
    import sys
    home = Path.home()
    expected = home / ".claude"
    configured = os.environ.get("CLAUDE_CONFIG_DIR") or str(expected)
    profiles = expected / "profiles"
    if (sys.platform != "linux" or os.path.normpath(configured) != str(expected)
            or not profiles.is_dir() or any(path.is_symlink() for path in (profiles, *profiles.parents))):
        raise RepairPolicyError("unsupported_root")
    return _repair_controller_identity()


def _repair_controller_identity() -> dict:
    import os
    from .background_job_process import process_start_token
    pid = os.getpid()
    token = process_start_token(pid)
    if not token:
        raise RepairPolicyError("controller_unverifiable")
    return {"pid": pid, "token": token}


def _repair_controller_ended(active) -> bool:
    """Positive old-controller termination only; unreadability is not death."""
    import os
    from .background_job_process import process_start_token
    previous = active["controller"]
    current = process_start_token(previous["pid"])
    if current:
        return current != previous["token"]
    try:
        os.kill(previous["pid"], 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _repair_owner_frame(c=None):
    from . import janitor_store
    ids = sorted(janitor_store.janitor_agent_ids(c, template_id=ROLE, enabled_only=True))
    frame = tuple((aid, janitor_store.admission_fingerprint(aid, c)) for aid in ids)
    return frame if all(fp is not None for _, fp in frame) else None


def _repair_owner_snapshot(owner):
    from . import janitor_builtins, turn_queue
    stop = owner.get("_repair_stop_generation")
    if (type(stop) is not int or stop != turn_queue.user_stop_generation(owner["agent_id"])
            or turn_queue.is_paused(owner["agent_id"])):
        return None
    before = _repair_owner_frame()
    current = janitor_builtins.resolve(ROLE)  # Full views stay OUTSIDE the writer.
    after = _repair_owner_frame()
    if (before is None or before != after or not current
            or (current["agent_id"], current["generation"]) != (owner["agent_id"], owner["generation"])
            or current["options"].get("auto_repair_auth") is not True):
        return None
    expected = (current["revision"], current["generation"], 1, current["backend"], current["model"], current["effort"], 1)
    return after if dict(after).get(current["agent_id"]) == expected else None


def _repair_owner_matches(frame, claim_or_owner, c):
    from . import turn_queue
    if isinstance(claim_or_owner, dict):
        aid, stop = claim_or_owner["agent_id"], claim_or_owner["_repair_stop_generation"]
    else:
        aid, stop = claim_or_owner.owner_id, claim_or_owner.stop_generation
    return (frame is not None and frame == _repair_owner_frame(c)
            and stop == turn_queue.user_stop_generation(aid) and not turn_queue.is_paused(aid))


def _repair_key(profile: RepairProfile) -> str:
    return _auth_repair_provider().auth_repair.identity_key(profile)


def _repair_existing_salt() -> str:
    from . import settings_store
    salt = settings_store.get(hotseat_status.SALT_KEY)
    if not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{32}", salt):
        raise RepairPolicyError("invalid_identity_binding")
    return salt


def _repair_binding(salt: str) -> str:
    import hashlib
    return hashlib.sha256(salt.encode()).hexdigest()


def _repair_bound_key(salt: str, alias: str, *, email="", org="", alias_only=False) -> str:
    return _auth_repair_provider().auth_repair.bound_key(
        salt, alias, email=email, org=org, alias_only=alias_only)


def _repair_integer(value, maximum=253402300799000):
    return type(value) is int and 0 <= value <= maximum


class RepairLedger:
    """One atomic Host/profile reservation, consumed before any external effect.

    A reservation alone is not auth evidence: the worker must revalidate its
    private report/identity and current owner before invoking the primitive.
    No resolver, process wait, browser, provider or network runs under a writer.
    Unknown effects are never reclaimed by a TTL, even across process restart.
    """
    def _load(self, now):
        from . import settings_store
        raw = settings_store.get(REPAIR_STATE_KEY)
        if raw is None:
            return {"v": 1, "identity_binding": None, "clock": now, "day": now // _REPAIR_DAY_MS, "count": 0,
                    "active": None, "runs": [], "profiles": {}}
        try:
            if len(raw.encode()) > 65536:
                raise ValueError
            state = json.loads(raw)
            self._validate(state)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise RepairPolicyError("invalid_state") from None
        if state["clock"] > now:
            raise RepairPolicyError("clock_rollback")
        return state

    def _validate(self, state):
        if (not isinstance(state, dict) or set(state) != {"v", "identity_binding", "clock", "day", "count", "active", "runs", "profiles"}
                or type(state["v"]) is not int or state["v"] != 1
                or not _repair_integer(state["clock"]) or not _repair_integer(state["day"])
                or state["day"] > state["clock"] // _REPAIR_DAY_MS or not _repair_integer(state["count"], 3)
                or not isinstance(state["runs"], list) or len(state["runs"]) > 32
                or not all(isinstance(rid, str) and re.fullmatch(r"janitor-demand-[0-9a-f]{64}", rid) for rid in state["runs"])
                or len(set(state["runs"])) != len(state["runs"])
                or not isinstance(state["profiles"], dict) or len(state["profiles"]) > REPAIR_MAX_PROFILES):
            raise ValueError
        binding = state["identity_binding"]
        if ((not state["profiles"] and binding is not None) or
                (state["profiles"] and (not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding)))):
            raise ValueError
        fields = {"alias_key", "status", "attempts", "failures", "next_retry_at", "last_code", "day", "count", "updated_at", "nonce", "unverified"}
        for key, row in state["profiles"].items():
            if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                    or not isinstance(row, dict) or set(row) != fields
                    or not isinstance(row["alias_key"], str) or not re.fullmatch(r"[0-9a-f]{32}", row["alias_key"])
                    or row["status"] not in _REPAIR_STATUSES or row["last_code"] not in _REPAIR_CODES
                    or not all(_repair_integer(row[k]) for k in ("attempts", "failures", "next_retry_at", "day", "updated_at"))
                    or not _repair_integer(row["count"], 2) or row["day"] > state["day"]
                    or row["updated_at"] > state["clock"] or type(row["unverified"]) is not bool
                    or not isinstance(row["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", row["nonce"])):
                raise ValueError
        if state["count"] != sum(row["count"] for row in state["profiles"].values() if row["day"] == state["day"]):
            raise ValueError
        if any(row["failures"] > row["attempts"] or row["count"] > row["attempts"] for row in state["profiles"].values()):
            raise ValueError
        active = state["active"]
        if active is not None:
            if (not isinstance(active, dict) or set(active) != {"key", "nonce", "owner_id", "generation", "run_id", "controller", "phase", "stop_generation"}
                    or active["key"] not in state["profiles"] or active["phase"] not in _REPAIR_PHASES
                    or active["nonce"] != state["profiles"][active["key"]]["nonce"]
                    or not isinstance(active["owner_id"], str) or not re.fullmatch(r"[0-9a-f]{16}", active["owner_id"])
                    or not _repair_integer(active["generation"]) or not _repair_integer(active["stop_generation"])
                    or active["run_id"] not in state["runs"]
                    or not isinstance(active["controller"], dict) or set(active["controller"]) != {"pid", "token"}
                    or not _repair_integer(active["controller"]["pid"], 2**31-1) or active["controller"]["pid"] == 0
                    or not isinstance(active["controller"]["token"], str)
                    or not re.fullmatch(r"[0-9a-f-]{36}:[0-9]+", active["controller"]["token"])
                    or active["phase"] != "reserved" and not state["profiles"][active["key"]]["unverified"]):
                raise ValueError

    def _save(self, state, now):
        from . import settings_store
        state["clock"] = now
        self._validate(state)
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise RepairPolicyError("state_capacity")
        settings_store.set_text(REPAIR_STATE_KEY, encoded, updated_at=now)

    def _failed(self, row, code, now):
        row.update(status="failed", last_code=code, failures=row["failures"] + 1, updated_at=now)
        row["next_retry_at"] = now + (6 if row["failures"] == 1 else 24) * _REPAIR_HOUR_MS

    def _same(self, active, claim):
        return bool(active and all(active[field] == getattr(claim, field)
                                   for field in ("key", "nonce", "owner_id", "generation", "run_id", "stop_generation")))

    def reserve(self, profile: RepairProfile, owner: dict, run_id: str, *, source: _RepairSource):
        from . import db, janitor_store, settings_store
        import secrets
        if owner["options"].get("auto_repair_auth") is not True:
            return None  # OFF: no claims, state reads, process/identity lookup or effects.
        if not isinstance(source, _RepairSource) or not source.current():
            return None
        controller = _repair_process_identity()
        if db.conn().in_transaction:
            raise RepairPolicyError("nested_transaction")
        frame = _repair_owner_snapshot(owner)
        if frame is None or not source.current():
            return None
        run = janitor_store.run_row(run_id)  # Immutable identity fields; no large JSON under writer.
        if (run is None or run["agent_id"] != owner["agent_id"] or run["generation"] != owner["generation"]
                or run["status"] not in {"completed", "failed"}):
            return None
        observed_state = self._load(db.now_ms())
        observed = observed_state["active"]
        ended = bool(observed and _repair_controller_ended(observed))
        if not observed_state["profiles"]:
            _repair_key(profile)  # First admission may create the canonical salt.
        salt = _repair_existing_salt()
        binding = _repair_binding(salt)
        if observed_state["profiles"] and observed_state["identity_binding"] != binding:
            raise RepairPolicyError("invalid_identity_binding")
        key = _repair_bound_key(salt, profile.alias, email=profile.email, org=profile.org_uuid)
        nonce = secrets.token_hex(16)
        with settings_store.transaction():
            now = db.now_ms()
            if (not source.current() or not _repair_owner_matches(frame, owner, db.conn())
                    or not 0 <= now - run["created_at"] <= 120_000):
                return None
            state = self._load(now)
            if _repair_existing_salt() != salt or state["profiles"] and state["identity_binding"] != binding:
                raise RepairPolicyError("invalid_identity_binding")
            active = state["active"]
            if active is not None:
                if active != observed or not ended:
                    return None
                row = state["profiles"][active["key"]]
                if row["status"] == "repairing":
                    self._failed(row, "interrupted", now)
                if active["phase"] not in {"reserved", "cleaned"}:
                    active["phase"] = "uncertain"
                    row["unverified"] = True
                    self._save(state, now)
                    return None
                state["active"] = None
                self._save(state, now)
            if state["day"] < now // _REPAIR_DAY_MS:
                state.update(day=now // _REPAIR_DAY_MS, count=0)
            row = state["profiles"].get(key)
            if (state["count"] >= 3 or run_id in state["runs"] or row and
                    (row["next_retry_at"] > now or row["status"] == "needs_user" or row["unverified"]
                     or row["day"] == state["day"] and row["count"] >= 2)):
                return None
            if row is None:
                if len(state["profiles"]) >= REPAIR_MAX_PROFILES:
                    raise RepairPolicyError("state_capacity")
                row = {"alias_key": _repair_bound_key(salt, profile.alias, alias_only=True), "status": "repairing", "attempts": 0, "failures": 0, "next_retry_at": 0,
                       "last_code": "started", "day": state["day"], "count": 0, "updated_at": now,
                       "nonce": nonce, "unverified": False}
                state["profiles"][key] = row
                state["identity_binding"] = binding
            if row["day"] != state["day"]:
                row.update(day=state["day"], count=0)
            row.update(status="repairing", attempts=row["attempts"] + 1, count=row["count"] + 1,
                       last_code="started", nonce=nonce, updated_at=now)
            state["count"] += 1
            state["runs"] = [*state["runs"], run_id][-32:]
            state["active"] = {"key": key, "nonce": nonce, "owner_id": owner["agent_id"],
                               "generation": owner["generation"], "run_id": run_id,
                               "controller": controller, "phase": "reserved",
                               "stop_generation": owner["_repair_stop_generation"]}
            self._save(state, now)
        return RepairClaim(key, nonce, owner["agent_id"], owner["generation"], run_id, owner["_repair_stop_generation"])

    def phase(self, claim: RepairClaim, phase: str) -> bool:
        from . import db, settings_store
        if phase not in _REPAIR_PHASES or db.conn().in_transaction:
            raise RepairPolicyError("invalid_phase")
        owner = {"agent_id": claim.owner_id, "generation": claim.generation,
                 "_repair_stop_generation": claim.stop_generation}
        frame = _repair_owner_snapshot(owner) if phase == "spawn_pending" else None
        controller = _repair_controller_identity()
        with settings_store.transaction():
            now = db.now_ms()
            state = self._load(now)
            if not self._same(state["active"], claim) or state["active"]["controller"] != controller:
                return False
            if phase == "spawn_pending" and not _repair_owner_matches(frame, claim, db.conn()):
                return False
            old = state["active"]["phase"]
            allowed = {"reserved": {"spawn_pending"}, "spawn_pending": {"running", "uncertain"},
                       "running": {"cleaned", "uncertain"}, "uncertain": set(), "cleaned": set()}
            if phase not in allowed[old]:
                raise RepairPolicyError("invalid_phase")
            state["active"]["phase"] = phase
            state["profiles"][claim.key].update(unverified=True, updated_at=now)
            self._save(state, now)
        return True

    def finish(self, claim: RepairClaim, code: str, *, cleanup_confirmed: bool, stop_event=None) -> bool:
        from . import db, settings_store
        if code not in _REPAIR_CODES - {"started", "verified"} or type(cleanup_confirmed) is not bool or db.conn().in_transaction:
            raise RepairPolicyError("invalid_outcome")
        # A late old owner may settle its own resource claim to FAILED, never
        # promote success or mutate another nonce's current attempt.
        owner = {"agent_id": claim.owner_id, "generation": claim.generation,
                 "_repair_stop_generation": claim.stop_generation}
        frame = _repair_owner_snapshot(owner)
        controller = _repair_controller_identity()
        with settings_store.transaction():
            now = db.now_ms()
            state = self._load(now)
            if not self._same(state["active"], claim) or state["active"]["controller"] != controller:
                return False
            row = state["profiles"][claim.key]
            if row["status"] != "repairing":
                if state["active"]["phase"] != "uncertain" or not cleanup_confirmed:
                    return False
                state["active"] = None
                row["updated_at"] = now
                self._save(state, now)
                return True
            current = _repair_owner_matches(frame, claim, db.conn()) and not (stop_event is not None and stop_event.is_set())
            effective = code if current else "interrupted"
            self._failed(row, effective, now)
            row["unverified"] = code in {"repaired", "already_ok"} or not cleanup_confirmed
            if effective in {"repaired", "already_ok"} and cleanup_confirmed:
                row["status"] = "repaired_unverified"
            elif effective in {"needs_user", "identity_mismatch"}: row["status"] = "needs_user"
            elif effective == "cooldown": row["status"] = "cooldown"
            if cleanup_confirmed: state["active"] = None
            else: state["active"]["phase"] = "uncertain"
            self._save(state, now)
        return True


    def quarantine(self, accounts: list[dict]) -> list[dict]:
        """Approved OFF exception: read debt only, never verify or mutate it.

        A never-enabled install reads the one bounded state key and returns
        the identical parsed accounts. Existing debt uses its canonical private
        alias binding; alias/identity replacement cannot silently clear it.
        If that binding cannot be trusted, preserve the working default and
        fail this provider's automatic alternatives closed, with codes only.
        """
        from . import db
        try:
            state = self._load(db.now_ms())
            protected = {row["alias_key"] for row in state["profiles"].values()
                         if row["unverified"] or row["status"] == "repaired_unverified"}
            if not protected:
                return accounts
            # Do NOT call identity_key/_salt here: its missing-salt recovery is
            # a write. Use only the original canonical identity binding.
            salt = _repair_existing_salt()
            if state["identity_binding"] != _repair_binding(salt):
                raise RepairPolicyError("invalid_identity_binding")
            def withheld(account):
                return _repair_bound_key(salt, account["alias"], alias_only=True) in protected
            return [({**account, "remaining": None, "secondary": None}
                     if not account["active"] and withheld(account) else dict(account)) for account in accounts]
        except Exception:
            _repair_diagnostic("debt_quarantine_unavailable")
            return [dict(account) if account["active"] else {**account, "remaining": None, "secondary": None}
                    for account in accounts]

    def candidates(self, accounts: list[dict], payload, *, owner: dict,
                   requested_model: str, read_origin: _RepairRead) -> list[dict]:
        """Quarantine touched aliases until this read independently proves them.

        Identity drift stays attached to the alias HMAC, never borrowing the
        new identity's quota to settle the old attempt. A previous model proof
        is not authority for a different requested model or a stale new read.
        The working active account is never removed or modified here.
        """
        from . import db, settings_store
        observed = self._load(db.now_ms())
        protected = {row["alias_key"] for row in observed["profiles"].values()
                     if row["unverified"] or row["status"] in {"repairing", "repaired"}}
        if not protected:
            return accounts
        salt = _repair_existing_salt()
        if observed["identity_binding"] != _repair_binding(salt):
            raise RepairPolicyError("invalid_identity_binding")
        profiles = {profile.alias: profile for profile in _saved_repair_profiles(payload)}
        source = _RepairSource.capture(payload, read_origin)
        try:
            if source is None:
                raise RepairPolicyError("verification_source_expired")
            _repair_process_identity()  # Default-root verification only, like admission.
        except RepairPolicyError as exc:
            _repair_diagnostic("verification_source_expired" if exc.code == "verification_source_expired"
                               else "verification_root_unavailable")
            frame = None
        else:
            frame = _repair_owner_snapshot(owner)
        proofs = {}
        now = db.now_ms()
        for profile in profiles.values():
            key = _repair_bound_key(salt, profile.alias, email=profile.email, org=profile.org_uuid)
            row = observed["profiles"].get(key)
            if (row and source is not None and source.current() and row["alias_key"] in protected
                    and source.read_started_ms >= row["updated_at"] and
                    repair_profile_usable(payload, profile, requested_model=requested_model,
                                          not_before_ms=source.read_started_ms, now_ms=now)):
                proofs[key] = row
        eligible = set()
        # No writer surrounds identity/model checks or owner resolution. Under
        # the writer bind only the current row/nonce and owner/Stop generation.
        if proofs and frame is not None and source is not None:
            if db.conn().in_transaction:
                raise RepairPolicyError("nested_transaction")
            with settings_store.transaction():
                now = db.now_ms()
                state = self._load(now)
                if (source.current() and _repair_owner_matches(frame, owner, db.conn()) and _repair_existing_salt() == salt
                        and state["identity_binding"] == observed["identity_binding"]):
                    for key, observed_row in proofs.items():
                        row = state["profiles"].get(key)
                        if row != observed_row or state["active"] and state["active"]["key"] == key:
                            continue
                        # Do not let a second, unresolved identity at this
                        # alias be bypassed by proving just one of them.
                        if any(other_key != key and other["alias_key"] == row["alias_key"]
                               and other["unverified"] for other_key, other in state["profiles"].items()):
                            continue
                        row.update(status="repaired", last_code="verified", unverified=False, updated_at=now)
                        eligible.add(row["alias_key"])
                    if eligible:
                        self._save(state, now)
        return [({**account, "remaining": None, "secondary": None}
                 if not account["active"] and _repair_bound_key(salt, account["alias"], alias_only=True) in protected - eligible
                 else dict(account)) for account in accounts]


# The maintained skill owns auth, token validation and durable uncertain-child
# exclusion. The Host consumes only this closed receipt, never stderr or tokens.
_REPAIR_PRIMITIVE_CODES = frozenset({"repaired", "already_ok", "needs_user", "identity_mismatch",
                                     "transient_failure", "cooldown"})
# Closed pair semantics of the sealed maintained schema1 primitive. An
# impossible pair cannot be reduced to a clean, retryable negative outcome.
_REPAIR_DETAILS_BY_CODE = {
    "repaired": frozenset({"identity_verified"}),
    "already_ok": frozenset({"identity_verified"}),
    "needs_user": frozenset({"verification_required"}),
    "identity_mismatch": frozenset({"invalid_profile", "identity_changed"}),
    "cooldown": frozenset({"profile_busy", "active_profile", "token_or_identity_changed",
                            "activation_busy", "cleanup_uncertain"}),
    "transient_failure": frozenset({"cleanup_uncertain", "timeout", "exchange_failed", "child_failed",
        "invalid_exchange", "invalid_child_outcome", "interrupted", "controller_failed",
        "publication_unverified", "unsupported_root"}),
}

REPAIR_ATTEMPT_SECONDS = 120
REPAIR_OUTPUT_BYTES = 4096


@dataclass(frozen=True)
class RepairResult:
    code: str
    cleanup_confirmed: bool


def _repair_diagnostic(code: str) -> None:
    hotseat_status.diagnose("janitorAuthRepair", f"code={code}")


def parse_repair_result(raw: bytes) -> RepairResult:
    """Strict schema, bounded output, no duplicate fields or raw diagnostics."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        if len(raw) > REPAIR_OUTPUT_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (not isinstance(value, dict) or set(value) != {"schema", "code", "detail_code", "cleanup_confirmed"}
                or value["schema"] != "clarp.claude-login-repair/1"
                or not isinstance(value["code"], str) or value["code"] not in _REPAIR_PRIMITIVE_CODES
                or not isinstance(value["detail_code"], str) or value["detail_code"] not in _REPAIR_DETAILS_BY_CODE[value["code"]]
                or type(value["cleanup_confirmed"]) is not bool
                or value["detail_code"] in {"publication_unverified", "cleanup_uncertain"} and value["cleanup_confirmed"]
                or value["code"] in {"repaired", "already_ok"} and
                    (not value["cleanup_confirmed"] or value["detail_code"] != "identity_verified")):
            raise ValueError
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise RepairPolicyError("invalid_primitive_receipt") from None
    return RepairResult(value["code"], value["cleanup_confirmed"])


def invoke_repair(profile: RepairProfile, *, source: _RepairSource, cancelled: Callable[[], bool],
                  started: Callable[[], bool]) -> RepairResult:
    """One owned process, finite output and one 120 s operational envelope.

    Reserve ten seconds of that envelope for cooperative TERM then group KILL
    and reap. A forced/uncertain cleanup is never success: the durable Host
    claim and maintained primitive journal remain closed, even after restart.
    This is not a claim that arbitrary detached browser/kernel work is killed.
    No retry, new profile, activation or raw child output is accepted here.
    """
    import os
    import selectors
    import signal
    import time
    from .process_registry import TurnHandle
    deadline = time.monotonic() + REPAIR_ATTEMPT_SECONDS
    if cancelled() or not isinstance(source, _RepairSource) or not source.current():
        return RepairResult("interrupted", True)
    # Linux default-root repair has a private wait owner. A non-reaping child
    # observation retains the leader's PID reservation through the LAST group
    # signal. Refuse auto-reaping/custom SIGCHLD policy rather than assuming it.
    if (not all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "CLD_EXITED"))
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        _repair_diagnostic("primitive_wait_ownership_unavailable")
        return RepairResult("transient_failure", True)  # No child was started.
    process = None
    handle = None
    reserved = True
    output = bytearray()
    result = RepairResult("transient_failure", False)
    def observe():
        nonlocal reserved
        if not reserved:
            raise RepairPolicyError("leader_ownership_lost")
        try:
            # WNOWAIT is essential: poll()/wait() would release this identity.
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            reserved = False
            raise RepairPolicyError("leader_ownership_lost") from None
    try:
        # Final source check AFTER potentially waiting cancellation/ownership
        # checks, immediately before issuing an effect. No resampled CLI read.
        if not source.current():
            return RepairResult("interrupted", True)
        process = subprocess.Popen(
            ["claude-login", "repair", profile.alias, "--expected-email", profile.email,
             "--expected-org", profile.org_uuid, "--json"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
        handle = TurnHandle(process, None, process_group=process.pid)
        if not started():
            raise RepairPolicyError("cancelled_before_running")
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while True:
                exited = observe()
                if eof and exited is not None:
                    if exited.si_code == os.CLD_EXITED and exited.si_status == 0:
                        result = parse_repair_result(bytes(output))
                    else:
                        _repair_diagnostic("primitive_exit")
                    break
                if cancelled():
                    result = RepairResult("interrupted", False)
                    break
                remaining = deadline - 10 - time.monotonic()
                if remaining <= 0:
                    _repair_diagnostic("primitive_timeout")
                    break
                for key, _ in selector.select(min(0.2, remaining)):
                    chunk = os.read(key.fd, REPAIR_OUTPUT_BYTES + 1 - len(output))
                    if not chunk:
                        eof = True
                        selector.unregister(process.stdout)
                    else:
                        output.extend(chunk)
                        if len(output) > REPAIR_OUTPUT_BYTES:
                            raise RepairPolicyError("primitive_output_limit")
    except Exception:
        # Class name, argv, exception strings and stderr are deliberately not
        # persisted or shown. A spawn exception can itself leave uncertainty.
        _repair_diagnostic("primitive_boundary_failed")
        result = RepairResult("transient_failure", False)
    finally:
        if handle is not None:
            try:
                observe()  # Never signal a group after its identity was lost.
                if not result.cleanup_confirmed and handle.terminate():
                    # With no positive primitive cleanup receipt, allow the
                    # owned group its grace, then KILL while its leader remains
                    # unreaped. Even a zombie leader still reserves this PID.
                    stop_at = min(deadline - 2, time.monotonic() + 6)
                    while time.monotonic() < stop_at:
                        observe()
                        time.sleep(min(0.05, max(0, stop_at - time.monotonic())))
                    observe()
                    handle.kill()
            except Exception:
                _repair_diagnostic("primitive_cleanup_uncertain")
                result = RepairResult("transient_failure", False)
            # All group operations are over BEFORE wait releases the identity.
            # ECHILD/foreign reap is UNKNOWN, never a reason to signal again.
            try:
                if reserved:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                elif process.returncode is None:
                    # Retire this private Popen handle without another numeric
                    # PID operation (including its destructor's implicit poll).
                    # sys.maxsize is bookkeeping UNKNOWN, as in Popen's own
                    # destructor; it is NEVER reported as a child exit status.
                    import sys
                    process.returncode = sys.maxsize
            except Exception:
                _repair_diagnostic("primitive_reap_uncertain")
                result = RepairResult("transient_failure", False)
            finally:
                process.stdout.close()
    return result


def _repair_fresh_report(payload, read_started_ms: int, now_ms: int) -> bool:
    stamp = payload.get("generated_at") if isinstance(payload, dict) else None
    return (_repair_integer(read_started_ms) and _repair_integer(now_ms) and 0 <= now_ms - read_started_ms <= 60_000
            and not isinstance(stamp, bool) and isinstance(stamp, (int, float))
            and read_started_ms - 1000 <= stamp * 1000 <= now_ms + 1000)


def read_repair_accounts(command: str) -> tuple[list[dict], dict, _RepairRead]:
    """Private view of the same scheduled Claude read, never the public GET.

    OFF uses read_accounts unchanged. This payload lives only for this worker
    offer/current selection; no email or UUID is added to a status/audit field.
    """
    from . import db
    import time
    started = _RepairRead(db.now_ms(), time.monotonic())
    spec = _auth_repair_provider()
    proc = _run(command, list(spec.read_args), spec.read_timeout)
    if proc.returncode or len(proc.stdout.encode("utf-8")) > 1_048_576:
        raise RepairPolicyError("private_read_failed")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list) or len(payload["accounts"]) > REPAIR_MAX_PROFILES:
        raise RepairPolicyError("invalid_private_read")
    hotseat_status.record_read(spec.auth_repair.identity_provider, payload)
    return spec.parse(payload), payload, started


def repair_requested_model() -> str:
    """Use the maintained runtime default; never a Janitor's own Codex model.

    The model must match the authoritative report's scope identifier exactly
    (case aside). A display label/model-id mismatch stays unverified rather
    than guessing family aliases or probing a model on repaired credentials.
    """
    return _auth_repair_provider().auth_repair.requested_model()


@dataclass(frozen=True)
class _RepairRead:
    started_ms: int
    started_mono: float


@dataclass(frozen=True)
class _RepairSource:
    """Immutable original read evidence; never refreshed by worker scheduling."""
    read_started_ms: int
    generated_at: int | float
    offered_at_ms: int
    offered_mono: float
    deadline_mono: float

    @classmethod
    def capture(cls, payload, read_origin):
        from . import db
        import time
        now = db.now_ms()
        mono = time.monotonic()
        if (not isinstance(read_origin, _RepairRead)
                or not _repair_fresh_report(payload, read_origin.started_ms, now)
                or isinstance(read_origin.started_mono, bool) or not isinstance(read_origin.started_mono, (int, float))
                or not 0 <= read_origin.started_mono <= 1e15
                or isinstance(mono, bool) or not isinstance(mono, (int, float)) or not 0 <= mono <= 1e15
                or not 0 <= mono - read_origin.started_mono < 60):
            return None
        return cls(read_origin.started_ms, payload["generated_at"], now, mono,
                   read_origin.started_mono + 60)

    def current(self) -> bool:
        from . import db
        import time
        now, mono = db.now_ms(), time.monotonic()
        return (_repair_integer(now) and now >= self.offered_at_ms
                and not isinstance(mono, bool) and isinstance(mono, (int, float))
                and self.offered_mono <= mono < self.deadline_mono
                and _repair_fresh_report({"generated_at": self.generated_at}, self.read_started_ms, now))


class RepairWorker:
    """One bounded serial repair, outside GET and ordinary Janitor demand runs.

    No queue: a busy worker drops an offer. The next scheduled Hotseat read is
    the independent quota/model verification; this worker never switches or
    sends a notification, and its result cannot resurrect a stopped run.
    """
    def __init__(self, stop_event):
        import threading
        self.stop_event = stop_event
        self._slot = threading.Lock()
        self.thread = None
        self.profiles = ()  # Immutable identities of this owned in-flight offer.

    def offer(self, owner: dict, run_id: str, payload, *, read_origin: _RepairRead) -> bool:
        import threading
        if owner["options"].get("auto_repair_auth") is not True or self.stop_event.is_set():
            return False
        source = _RepairSource.capture(payload, read_origin)
        if source is None:
            return False
        profiles = repair_profiles(payload)
        if not profiles or not self._slot.acquire(blocking=False):
            return False
        # Retain only immutable, validated identities; not the private report.
        self.profiles = profiles
        try:
            self.thread = threading.Thread(target=self._work, args=(dict(owner), run_id, profiles, source),
                                           name="janitor-hotseat-auth-repair", daemon=True)
            self.thread.start()
        except Exception:
            self.profiles = ()
            self._slot.release()
            _repair_diagnostic("worker_start_failed")
            return False
        return True

    def _work(self, owner, run_id, profiles, source):
        from . import db
        ledger, claim = RepairLedger(), None
        result = RepairResult("transient_failure", False)
        try:
            frame = _repair_owner_snapshot(owner)
            def cancelled():
                return self.stop_event.is_set() or not _repair_owner_matches(frame, owner, db.conn())
            for profile in profiles:
                if cancelled() or not source.current():
                    _repair_diagnostic("source_expired_before_admission")
                    return
                claim = ledger.reserve(profile, owner, run_id, source=source)
                if claim is not None:
                    break
            if claim is None:
                return
            if cancelled() or not source.current() or not ledger.phase(claim, "spawn_pending"):
                result = RepairResult("interrupted", True)
            else:
                result = invoke_repair(profile, source=source, cancelled=cancelled,
                                       started=lambda: not cancelled() and ledger.phase(claim, "running"))
                # Keep the immutable auth/publication fact. finish() applies
                # current Stop/owner/service fences to the outcome separately;
                # resource cleanup never revokes possible publication debt.
        except RepairPolicyError as exc:
            _repair_diagnostic("unsupported_root" if exc.code == "unsupported_root" else "policy_refused")
        except Exception:
            _repair_diagnostic("worker_failed")
        finally:
            try:
                if claim is not None:
                    ledger.finish(claim, result.code, cleanup_confirmed=result.cleanup_confirmed,
                                  stop_event=self.stop_event)
            except Exception:
                # Do not retry a possibly committed settlement. Its durable
                # reserved/uncertain claim stays fail-closed for reconciliation.
                _repair_diagnostic("settlement_uncertain")
            finally:
                try:
                    db.close_local()
                except Exception:
                    _repair_diagnostic("worker_connection_close_failed")
                finally:
                    self.profiles = ()
                    self._slot.release()


def repair_preview() -> str:
    """Closed status counts on the existing Janitor line, never raw identities."""
    from . import db
    state = RepairLedger()._load(db.now_ms())
    counts = {status: sum(row["status"] == status for row in state["profiles"].values())
              for status in sorted(_REPAIR_STATUSES)}
    parts = [f"{status}: {count}" for status, count in counts.items() if count]
    return "Auth repair: " + ", ".join(parts) if parts else ""
