"""SendRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "send"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
from lib import agents as agents_db
from lib import trace as _trace
from lib import events
from lib import identity
from lib.log import log_exception
from lib.send_request import SendRequest, SendRequestError
from lib.orchestrator import get_settings as get_orchestrator_settings, recent_ignored_decisions as recent_orchestrator_ignored_decisions, recent_decisions as recent_orchestrator_decisions
from lib.routes.helpers import _runtime_paths


class SendRoutes:
    def _apply_call_opening(self, req, call_opening):
        """A listen-first greeting (docs/agent-rings.md "Listen-first"): the
        utterance goes to the exact calling agent, never through selection or
        the orchestrator. Returns (request, opening result) or (None, None)
        after answering the refusal; a refusal dispatches nothing."""
        from dataclasses import replace
        from lib import agent_rings, group_calls
        from lib.http_utils import principal_of
        if (not isinstance(call_opening, dict) or not isinstance(call_opening.get("token"), str)
                or not req.call_id):
            self._json_error(400, "bad call_opening")
            return None, None
        agent_rings.bind(self.ctx)
        group_calls.bind(self.ctx)
        try:
            result = agent_rings.greeting_routed(
                principal_of(self).principal, token=call_opening.get("token"),
                epoch=call_opening.get("epoch"), client_msg_id=req.client_msg_id,
                trace_id=req.trace_id, session=req.session, call_id=req.call_id)
        except group_calls.CallError as exc:
            self._json(exc.status, exc.body())
            return None, None
        return replace(req, session=result["session"], force_session=True), result

    def _handle_compact(self):
        """Compact a conversation on demand (from the app). Drives the backend
        CLI's interactive /compact (claude/codex) or /compress (agy) in a
        throwaway tmux session resumed on the same session id, keeping the id.
        Body: {"session": "..."}. Returns immediately; the app watches the
        snapshot's `compacting` flag + `context_tokens` for progress."""
        from lib import compaction
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        result = compaction.compact_session(session)
        status = 200 if result.get("ok") else 409
        return self._json(status, result)

    def _handle_orchestrator_addressing(self):
        """Choose how an unaddressed spoken turn picks its agent.

        `ai` is the default and the only mode that ever calls the model, so
        the others are also the cheap ones.
        """
        from lib import addressing
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            chosen = addressing.set_mode(str(data.get("mode") or ""))
        except ValueError:
            return self._json(400, {
     "error": "unknown addressing mode",
     "modes": list(addressing.MODES),
 })
        return self._json_ok({
            "ok": True, "mode": chosen, "modes": list(addressing.MODES),
        })

    def _handle_orchestrator_decisions(self):
        qs = self._query()
        try:
            limit = int(qs.get("limit", ["30"])[0])
        except ValueError:
            limit = 30
        self._json_ok({"decisions": recent_orchestrator_decisions(limit)})

    def _handle_orchestrator_route_delegation(self):
        self._orchestrator_fallback_request = True
        return self._handle_send()

    def _handle_orchestrator_settings_get(self):
        from lib.orchestrator import provider_options
        body = json.dumps({
            "settings": get_orchestrator_settings().__dict__,
            "providers": provider_options(),
            "recent_decisions": recent_orchestrator_decisions(5),
            "ignored_decisions": recent_orchestrator_ignored_decisions(20),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_select(self):
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session or not all(c.isalnum() or c in "._-" for c in session):
            return self._json_error(400, "bad session name")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such session")
        from lib import client_chat
        client_chat.record(self.principal().principal, session)
        # Persist focus in the DB (what transcribe, upload and the herald read
        # via lib.focus) and mirror it into the small focus-state file kept for
        # anything outside this process.
        herald = getattr(self.ctx, "herald", None)
        if agent and herald is not None:
            try:
                with agents_db.focus_guard():
                    agents_db.set_focus(agent["agent_id"])
                    herald.set_focus(session)
            except Exception as e:  # noqa: BLE001
                log_exception("focusDbWriteFail", e, detail=session)
        elif agent:
            try:
                agents_db.set_focus(agent["agent_id"])
            except Exception as e:  # noqa: BLE001
                log_exception("focusDbWriteFail", e, detail=session)
        state_path = _runtime_paths().app_session
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(session + "\n")
        except OSError as e:
            log_exception("selectStateWriteFail", e, detail=str(state_path))
            return self._json_error(500, "could not persist selection")
        # Tell the herald manager the user's focus changed — if that agent
        # had a pending herald, their held buffer drains now.
        # Push focus change so the client doesn't have to poll for it.
        events.broadcast(self.ctx.stream, events.agent_focus(
            session=session, agent_id=(agent or {}).get("agent_id")))
        self._json_ok({"ok": True, "session": session})

    def _handle_send(self):
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._send(400, b"bad json")
        try:
            req = SendRequest.from_payload(
                data,
                default_session=self.ctx.default_session,
                trace_id_factory=_trace.new_trace_id,
                authenticated=bool(
                    getattr(self, "_request_auth_validated", False)
                    and (getattr(self.ctx, "auth_token", "") or "")
                ),
                orchestrator_fallback=getattr(
                    self, "_orchestrator_fallback_request", False),
                caller_agent_id=self._turn_caller(),
            )
        except SendRequestError as e:
            return self._send_request_error(e)
        self._trace_id = req.trace_id
        # The admission row is filed before the empty-text refusal so a
        # rejected send is still accounted for.
        prompt_admission = req.admit()
        try:
            req.require_text()
        except SendRequestError as e:
            return self._send_request_error(e)
        opening = None
        if "call_opening" in data:
            req, opening = self._apply_call_opening(req, data["call_opening"])
            if req is None:
                return
        if opening is None:
            req, call_sessions = self._apply_group_call(req)
            if req is None:
                return
        else:
            call_sessions = None                  # routed with the greeting, as one decision
        if not req.force_session:
            handled = self._send_via_orchestrator(req, prompt_admission, only_sessions=call_sessions)
            if handled:
                return
        self._send_direct(req, prompt_admission, opening=opening)

    def _send_request_error(self, e: SendRequestError) -> None:
        if e.trace_id:
            self._trace_id = e.trace_id
        if e.json_body:
            return self._json_error(e.status, str(e))
        return self._send(e.status, str(e).encode())
