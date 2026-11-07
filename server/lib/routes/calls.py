"""CallRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "calls"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from lib import identity
from lib.log import log_exception


class CallRoutes:
    def _apply_group_call(self, req):
        """A /send in a group call: (request routed to a reachable participant,
        the participants the orchestrator may pick), or (None, None) after
        answering 409. A send without ``call_id`` passes through unchanged."""
        if not req.call_id:
            return req, None
        from dataclasses import replace
        from lib import group_calls
        from lib.http_utils import principal_of
        group_calls.bind(self.ctx)
        try:
            routed = group_calls.route(principal_of(self).principal, req.call_id, req.session)
        except group_calls.CallError as exc:
            self._json(exc.status, exc.body())
            return None, None
        return replace(req, session=routed["session"]), tuple(routed["reachable"])

    def _calls_by(self, data: dict) -> str:
        """The agent making a change: its turn identity, else the session it names."""
        from lib import turn_identity
        from lib.provider_background_jobs import TURN_ENV
        try:
            agent_id = turn_identity.caller_agent_id(
                {TURN_ENV: (getattr(self, "headers", None) or {}).get("X-Clarp-Turn", "")})
        except Exception as exc:  # noqa: BLE001 - attribution only: logged, then unattributed
            log_exception("callsByLookupFail", exc)
            agent_id = ""
        agent = identity.lookup(agent_id) if agent_id else None
        if agent:
            return str(agent["session"])
        named = identity.lookup(str(data.get("by") or "").strip()) if data.get("by") else None
        return str(named["session"]) if named else ""

    def _calls_principal(self):
        """The full-scope caller, or None after answering 401."""
        from lib.http_utils import principal_of, require_full_scope
        who = principal_of(self)
        denied = require_full_scope(who, message="Group calls require full-device authentication")
        if denied:
            self._json_error(401, denied)
            return None
        return who.principal

    def _calls_ring_run(self, work, *, body: bool = True):
        from lib import agent_rings, group_calls
        principal = self._calls_principal()
        if principal is None:
            return
        data = self._read_json() if body else {}
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        agent_rings.bind(self.ctx)
        group_calls.bind(self.ctx)
        try:
            return self._json_ok(work(agent_rings, principal, data))
        except group_calls.CallError as exc:
            return self._json(exc.status, exc.body())

    def _handle_calls_action(self, action: str):
        from lib import group_calls
        principal = self._calls_principal()
        if principal is None:
            return
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        group_calls.bind(self.ctx)
        scope = {"call_id": str(data.get("call_id") or ""), "principal": str(data.get("principal") or ""),
                 "by": self._calls_by(data)}
        try:
            if action == "end":
                return self._json_ok(group_calls.end(principal, **scope))
            return self._json_ok(group_calls.change(principal, action, str(data.get("agent") or ""), **scope))
        except group_calls.CallError as exc:
            return self._json(exc.status, exc.body())

    def _handle_calls_add(self):
        return self._handle_calls_action("add")

    def _handle_calls_end(self):
        return self._handle_calls_action("end")

    def _handle_calls_get(self):
        from lib import group_calls
        principal = self._calls_principal()
        if principal is None:
            return
        wanted = (self._query().get("principal") or [""])[0] if principal == group_calls.ADMINISTRATOR else ""
        group_calls.bind(self.ctx)
        try:
            return self._json_ok(group_calls.snapshot(principal, principal=wanted))
        except group_calls.CallError as exc:
            return self._json(exc.status, exc.body())

    def _handle_calls_hold(self):
        return self._handle_calls_action("hold")

    def _handle_calls_live(self):
        def work(rings, principal, data):
            from lib import agent_ring_live as live
            live.require_device(principal)
            if set(data) != {"agent", "request_id", "client_nonce"}:
                raise rings.CallError(400, live.REQUEST_INVALID)
            return rings.user_call_live(principal, data["agent"], data["request_id"], data["client_nonce"])
        return self._calls_ring_run(work)

    def _handle_calls_remove(self):
        return self._handle_calls_action("remove")

    def _handle_calls_resume(self):
        return self._handle_calls_action("resume")

    def _handle_calls_ring(self):
        # ``opening_mode`` is passed only when sent: a ring without it is today's ring.
        return self._calls_ring_run(lambda rings, _p, data: rings.ring(
            caller_agent_id=self._turn_caller(), agent=str(data.get("agent") or ""),
            reason=str(data.get("reason") or ""), opening_line=str(data.get("opening_line") or ""),
            conversation_mode=data.get("conversation_mode"), event_context=data.get("event_context"),
            **({"opening_mode": data["opening_mode"]} if "opening_mode" in data else {})))

    def _handle_calls_ring_answer(self):
        return self._calls_ring_run(lambda rings, principal, data: rings.answer(
            principal, str(data.get("ring_id") or ""), conversation_mode=data.get("conversation_mode"),
            request_id=data.get("request_id"), client_nonce=data.get("client_nonce"),
            **({"opening_mode": data["opening_mode"]} if "opening_mode" in data else {})))

    def _handle_calls_ring_cancel(self):
        return self._calls_ring_run(lambda rings, _p, data: rings.cancel(
            self._turn_caller(), str(data.get("ring_id") or "")))

    def _handle_calls_ring_decline(self):
        return self._calls_ring_run(lambda rings, principal, data: rings.decline(
            principal, str(data.get("ring_id") or "")))

    def _handle_calls_ring_device(self):
        """POST /calls/ring/device {"receive_rings": bool, "push_token"?: "<this phone's APNs alert token>"}:
        this phone switches agent rings on or off for itself (docs/agent-rings.md)."""
        return self._calls_ring_run(lambda rings, principal, data: rings.set_receive_rings(principal, data))

    def _handle_calls_ring_device_get(self):
        return self._calls_ring_run(lambda rings, principal, _d: rings.get_device_state(principal), body=False)

    def _handle_calls_ring_get(self):
        def work(rings, principal, _data):
            ring_id = (self._query().get("ring_id") or [""])[0]
            ring = rings.get(ring_id) if ring_id else rings.current(principal)
            if ring is not None and not rings.event_visible(ring, principal) and \
                    ring.get("answered_by") != principal:
                ring = None                     # a Live ring is invisible to a phone that cannot run it (H6)
            if ring_id and ring is None:
                raise rings.CallError(404, "unknown call")
            return {"ring": ring, "settings": rings.settings()}
        return self._calls_ring_run(work, body=False)

    def _handle_calls_ring_live_close(self):
        from lib import agent_ring_live
        return self._calls_ring_run(lambda _r, principal, data: agent_ring_live.close(principal, data))

    def _handle_calls_ring_live_fallback(self):
        from lib import agent_ring_live
        return self._calls_ring_run(lambda _r, principal, data: agent_ring_live.fallback(principal, data))

    def _handle_calls_ring_opening(self):
        """A listen-first opening claim (docs/agent-rings.md "Listen-first")."""
        return self._calls_ring_run(lambda rings, principal, data: rings.opening_claim(principal, data))

    def _handle_calls_ring_settings_get(self):
        return self._calls_ring_run(lambda rings, _p, _d: {
            "ok": True, "settings": rings.settings(), "audit": rings.audit()[:50]}, body=False)

    def _handle_calls_ring_settings_post(self):
        return self._calls_ring_run(lambda rings, _p, data: {
            "ok": True, "settings": rings.update_settings(data), "audit": rings.audit()[:50]})

    def _handle_calls_ring_voip_token(self):
        return self._calls_ring_run(lambda rings, principal, data: rings.register_voip_token(
            principal, str(data.get("token") or ""), environment=str(data.get("environment") or ""),
            supported_conversation_modes=data.get("supported_conversation_modes")))

    def _handle_calls_start(self):
        from lib import group_calls
        principal = self._calls_principal()
        if principal is None:
            return
        data = self._read_json()
        if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
            return self._json_error(400, "agents must be a list")
        group_calls.bind(self.ctx)
        try:
            return self._json_ok(group_calls.start(
                principal, [str(a) for a in data["agents"]], request_id=str(data.get("request_id") or ""),
                principal=str(data.get("principal") or ""), by=self._calls_by(data)))
        except group_calls.CallError as exc:
            return self._json(exc.status, exc.body())

    def _handle_calls_switch(self):
        return self._handle_calls_action("switch")

    def _handle_calls_transfer(self):
        return self._handle_calls_action("transfer")
