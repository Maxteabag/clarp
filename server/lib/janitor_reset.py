"""Trusted, opt-in reset command transport. No credentials or quota guesses."""
from __future__ import annotations

import json
import re
import subprocess

from . import config
from .log import log_fault


def attempt(command_field: str, models: list[str]) -> dict:
    command = getattr(config.load(), command_field, ()) if command_field else ()
    if not command:
        return {"status": "disabled", "verified": False}
    try:
        result = subprocess.run(list(command), input=json.dumps({"models": models}),
                                capture_output=True, text=True, timeout=150, check=False)
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict):
            raise ValueError("unreadable reset receipt")
        verified = (result.returncode == 0 and receipt.get("verified") is True
                    and receipt.get("status") in {"redeemed", "reconciled"}
                    and isinstance(receipt.get("account"), str)
                    and re.fullmatch(r"ai(?:[1-9]|1[0-5])", receipt["account"]) is not None)
        status = receipt.get("status")
        allowed = {"disabled", "not_applicable", "no_owned_reset_accounts", "busy", "cooldown",
                   "unverified", "no_eligible_grant", "eligible", "redeemed", "reconciled"}
        if status not in allowed or result.returncode or (status in {"redeemed", "reconciled"} and not verified):
            status = "unverified"
        return {"status": status,
                "verified": verified, "account": receipt.get("account") if verified else None}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        log_fault("claudeBankedResetCommandFailed", exc)
        return {"status": "unverified", "verified": False}
