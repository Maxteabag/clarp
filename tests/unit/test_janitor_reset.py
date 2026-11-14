import json
import os
import sys
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib import janitor_reset


def test_reset_adapter_is_disabled_without_explicit_command():
    with patch.object(janitor_reset.config, "load", return_value=SimpleNamespace()), patch.object(janitor_reset.subprocess, "Popen") as run:
        assert janitor_reset.attempt("claude_reset_command", ["Fable"]) == {"status": "disabled", "verified": False, "needs_reconcile": False}
        run.assert_not_called()


@pytest.mark.parametrize("code,receipt,want", [
    (0, {"status":"redeemed","account":"ai2","verified":True}, True),
    (0, {"status":"reconciled","account":"ai15","verified":True}, True),
    (1, {"status":"redeemed","account":"ai2","verified":True}, False),
    (0, {"status":"redeemed","account":"ai2","verified":False}, False),
    (0, {"status":"redeemed","account":"ecit","verified":True}, False),
    (0, {"available":True}, False),
])
def test_reset_adapter_requires_structured_verified_receipt(code, receipt, want):
    class Process:
        pid = 123
        returncode = code
        def communicate(self, **kwargs):
            return json.dumps(receipt), ""
    with patch.object(janitor_reset.config,"load",return_value=SimpleNamespace(claude_reset_command=("broker",))), patch.object(janitor_reset.subprocess,"Popen",return_value=Process()) as run, patch.object(janitor_reset,"_kill_group"):
        result=janitor_reset.attempt("claude_reset_command",["claude-fable-5-2"])
        assert result["verified"] is want
        assert run.call_args.kwargs["start_new_session"] is True
        assert "shell" not in run.call_args.kwargs


def test_timeout_kills_command_descendants_and_retains_no_secret_text(tmp_path, monkeypatch, capsys):
    script=tmp_path/"hang.py";pidfile=tmp_path/"child.pid"
    script.write_text("import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\nopen(sys.argv[1],'w').write(str(p.pid))\nprint('SECRET-WOULD-BE-IN-OUTPUT',flush=True)\ntime.sleep(60)\n")
    monkeypatch.setattr(janitor_reset.config,"load",lambda:SimpleNamespace(claude_reset_command=(sys.executable,str(script),str(pidfile))))
    monkeypatch.setattr(janitor_reset,"TIMEOUT_SECONDS",.5)
    result=janitor_reset.attempt("claude_reset_command",["Fable"])
    assert result["verified"] is False and result['needs_reconcile'] is True
    pid=int(pidfile.read_text())
    # A killed descendant can briefly remain as a zombie until init reaps it.
    status=__import__('subprocess').run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
    assert not status.stdout.strip() or status.stdout.strip().startswith('Z')
    assert "SECRET-WOULD-BE-IN-OUTPUT" not in capsys.readouterr().err


def test_owner_cancellation_never_launches_a_command(monkeypatch):
    monkeypatch.setattr(janitor_reset.config,"load",lambda:SimpleNamespace(claude_reset_command=("never",)))
    with patch.object(janitor_reset.subprocess,"Popen") as spawn:
        assert janitor_reset.attempt("claude_reset_command",["Fable"],cancelled=lambda:True)["verified"] is False
        spawn.assert_not_called()


@pytest.mark.parametrize('status',['busy','cooldown','unverified','disabled','not_applicable'])
def test_read_only_failure_never_drops_the_existing_reconciliation_obligation(monkeypatch,status):
    class Process:
        pid=123;returncode=0
        def communicate(self,**kwargs):return json.dumps({'status':status,'verified':False}),''
    with patch.object(janitor_reset.config,'load',return_value=SimpleNamespace(claude_reset_command=('fake',))),patch.object(janitor_reset.subprocess,'Popen',return_value=Process()),patch.object(janitor_reset,'_kill_group'):
        result=janitor_reset.attempt('claude_reset_command',[],reconcile_only=True)
        assert result['needs_reconcile'] is True


def test_read_only_admission_cancel_or_missing_command_retains_debt():
    with patch.object(janitor_reset.config,'load',return_value=SimpleNamespace()):
        assert janitor_reset.attempt('claude_reset_command',[],reconcile_only=True)['needs_reconcile']
