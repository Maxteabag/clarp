"""GoalRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "goals"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from lib import agents as agents_db
from lib import backends
from lib import identity
from lib.log import log, log_exception


class GoalRoutes:
    def _goal_action(self, action: str):
        """Start, pause, resume or clear an agent's goal.

        Body: {"session": "...", "objective": "..."} (objective for start only).
        The backend owns the goal (Codex: thread/goal/*), so the call goes to
        the runtime that holds the app-server connection; the answer is the
        goal as the backend now reports it. Backends without goal control
        answer 501 rather than pretending.
        """
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        objective = str(data.get("objective") or "").strip()
        if action == "start" and not objective:
            return self._json_error(400, "objective required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        try:
            goal = backends.goal(
                str(agent.get("backend") or ""), agent["agent_id"], action,
                objective=objective, stream=self.ctx.stream)
        except backends.GoalUnsupported as exc:
            return self._json_error(501, str(exc))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - relay the backend's own refusal
            log_exception("agentGoalFail", exc, detail=f"{session}:{action}")
            return self._json_error(502, str(exc))
        return self._json_ok({
            "ok": True, "session": session, "action": action, "goal": goal,
        })

    def _handle_agent_goal_clear(self):
        return self._goal_action("clear")

    def _handle_agent_goal_get(self):
        """The goal an agent is working toward on its own, or null."""
        from lib import agent_goals
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        if not session:
            return self._json_error(400, "session required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        return self._json_ok({
            "session": session,
            "goal": agent_goals.public(agent_goals.get(agent["agent_id"])),
        })

    def _handle_agent_goal_pause(self):
        return self._goal_action("pause")

    def _handle_agent_goal_resume(self):
        return self._goal_action("resume")

    def _handle_agent_goal_start(self):
        return self._goal_action("start")

    def _handle_agent_helper_state(self):
        """Mark a helper agent done, failed or running again.

        ``by`` names the marking agent when an agent does it (only the
        helper's parent may); without it the mark is the user's."""
        from lib import helper_agents
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        session = str(data.get("session") or "").strip()
        state = str(data.get("state") or "").strip()
        if not session or not state:
            return self._json_error(400, "session and state required")
        try:
            new = helper_agents.mark(self.ctx.stream, session, state,
                                     by=str(data.get("by") or "").strip())
        except helper_agents.HelperStateError as e:
            return self._json(e.status, {"error": e.code, "message": str(e)})
        return self._json_ok({"ok": True, "session": session, "helper_state": new})

    def _handle_agent_helper_state_get(self):
        """One agent's lineage and helper state, without a full snapshot."""
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        if not session:
            return self._json_error(400, "session required")
        row = identity.lookup(session)
        row = agents_db.get_by_agent_id(row["agent_id"]) if row else None
        if not row:
            return self._json_error(404, "agent_not_found")
        return self._json_ok({key: row.get(key) for key in (
            "agent_id", "session", "role", "parent_agent_id", "helper_state",
            "helper_completed_at", "archived_at")})

    def _handle_goal_delegation_enable(self):
        """Make a helper the bookkeeping delegate of its parent and start its listener."""
        from lib import agents as agents_db, bookkeeping_listener, goal_ledger
        body = self._read_json()
        if body is None:
            return
        principal = identity.lookup(str(body.get("principal") or ""))
        delegate = identity.lookup(str(body.get("delegate") or ""))
        if not principal or not delegate:
            return self._json_error(404, "principal or delegate not found")
        try:
            result = goal_ledger.enable(
                agents_db.get_by_agent_id(principal["agent_id"]) or principal,
                agents_db.get_by_agent_id(delegate["agent_id"]) or delegate,
                str(body.get("reason") or ""))
        except goal_ledger.LedgerError as exc:
            return self._json_error(409, str(exc))
        result["listener"] = bookkeeping_listener.launch(result["delegation_id"])
        log("bookkeepingEnabled", f"delegation={result['delegation_id']} "
            f"principal={principal['session']} delegate={delegate['session']}")
        self._json_ok(result)

    def _handle_goal_delegation_stop(self):
        from lib import bookkeeping_listener, goal_ledger
        body = self._read_json()
        if body is None:
            return
        try:
            result = goal_ledger.stop(str(body.get("delegation_id") or ""),
                                      str(body.get("reason") or ""))
        except goal_ledger.LedgerError as exc:
            return self._json_error(409, str(exc))
        result["listener"] = bookkeeping_listener.stop_unit(result["delegation_id"])
        log("bookkeepingStopped", f"delegation={result['delegation_id']}")
        self._json_ok(result)

    def _handle_goal_delegations(self):
        from lib import goal_ledger
        session = (self._query().get("session", [""])[0] or "").strip()
        agent = identity.lookup(session) if session else None
        if not agent:
            return self._json_error(404, "agent not found")
        self._json_ok({"delegations": goal_ledger.delegations_for(agent["agent_id"])})

    def _handle_goal_ledger(self):
        from lib import goal_ledger
        query = self._query()
        plan_id = (query.get("plan_id", [""])[0] or "").strip()
        if not plan_id:
            return self._json_error(400, "plan_id required")
        try:
            after = int(query.get("after", ["0"])[0] or 0)
            limit = int(query.get("limit", ["200"])[0] or 200)
        except ValueError:
            return self._json_error(400, "after and limit are integers")
        self._json_ok(goal_ledger.events(plan_id, after=after, limit=limit))

    def _handle_task_goal_document(self):
        from lib import task_plans
        query=self._query()
        try:
            raw=query.get("revision", [None])[0]
            document=task_plans.goal_document(query.get("plan_id", [""])[0],
                query.get("name", [""])[0], int(raw) if raw is not None else None)
            self._json_ok({"document": document})
        except ValueError as exc:
            self._json_error(404, str(exc))

    def _handle_task_plan(self):
        from lib import task_plans
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        if not session:
            return self._json_error(400, "session required")
        self._json_ok({
            "plan": task_plans.active_for_session(session)
        })

    def _handle_task_plan_action(self):
        from lib import task_goal_state, task_plans
        body = self._read_json()
        if body is None:
            return
        try:
            plan_id = str(body.get("plan_id") or "")
            revision = body.get("revision")
            if not isinstance(revision, int):
                raise ValueError("revision required")
            from lib import goal_ledger
            # The apps call this on the user's behalf; agents use clarp-goal. A
            # request carrying an agent's turn identity is that agent, not the user.
            caller = self._turn_caller()
            actor = (goal_ledger.acting_as("owner", caller, verified=True) if caller
                     else goal_ledger.acting_as("user"))
            with actor:
                result = task_goal_state.mutate(plan_id, revision=revision,
                    action=str(body.get("action") or ""), data=body.get("data") or {})
            self._json_ok({"plan": result})
        except (ValueError, TypeError) as exc:
            self._json_error(409, str(exc))

    def _handle_task_plans(self):
        from lib import task_plans
        session = (self._query().get("session", [""])[0] or "").strip()
        if not session:
            return self._json_error(400, "session required")
        self._json_ok({"plans": task_plans.list_for_session(session)})
