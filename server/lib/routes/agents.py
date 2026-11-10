"""AgentRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "agents"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import pathlib
from urllib.parse import unquote
from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService
from lib import agents as agents_db
from lib import backends
from lib import events
from lib import identity
from lib.log import log
from lib.routes.helpers import _raw_query_value


class AgentRoutes:
    def _handle_agent_archive(self):
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        session = str(data.get("session") or "").strip()
        archived = data.get("archived")
        if not session or not isinstance(archived, bool):
            return self._json_error(400, "session and archived required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        agents_db.set_archived(agent["agent_id"], archived)
        return self._json_ok({"ok": True, "session": session,
                              "archived": archived})

    def _handle_agent_assign(self):
        from lib.contact_assignment import assign_contact
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        if self._reject_janitor_control(data):
            return
        try:
            result = assign_contact(str(data.get("session") or ""),
                                    str(data.get("mode") or "auto"), data.get("name"))
        except AgentLifecycleError as exc:
            return self._json(exc.status, exc.response())
        except ValueError as exc:
            return self._json_error(409, str(exc))
        if data.get("mode") != "options":
            events.broadcast(self.ctx.stream, events.agent_roster(
                "contact-assigned", session=result["session"], name=result["name"]))
        return self._json_ok(result)

    def _handle_agent_avatar(self, agent_id: str):
        row = identity.lookup(agent_id)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        self._send_file(path)

    def _handle_agent_conversations(self):
        """Pair conversations between agents, grouped by stable agent IDs."""
        from lib import agent_conversations
        body = json.dumps({"conversations": agent_conversations.list_conversations()}).encode()
        self._send(200, body, "application/json")

    def _handle_agent_file(self):
        from lib.agent_files import AgentFileError, read_text_file
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        relative = _raw_query_value(self.path, "path")
        requested_root = _raw_query_value(self.path, "root")
        if not session or not relative:
            return self._json_error(400, "session and path required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "agent not found")
        cwd = requested_root or str(agent.get("cwd") or "")
        if not cwd:
            return self._json_error(404, "agent workspace unavailable")
        try:
            root_candidate = pathlib.Path(cwd).expanduser()
            if requested_root and not root_candidate.is_absolute():
                return self._json_error(400, "root path must be absolute")
            # See `_handle_agent_files`: this is an operator capability, not a
            # sandbox boundary. Descendant traversal still uses no-follow FDs.
            root = root_candidate
            if not root.is_dir():
                return self._json_error(404, "agent workspace unavailable")
            payload = read_text_file(root, relative)
        except AgentFileError as exc:
            return self._json_error(exc.status, str(exc))
        except ValueError as exc:
            return self._json_error(403, str(exc))
        except (OSError, RuntimeError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok(payload)

    def _handle_agent_files(self):
        from lib.agent_files import AgentFileError, list_directory
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        relative = _raw_query_value(self.path, "path")
        requested_root = _raw_query_value(self.path, "root")
        if not session:
            return self._json_error(400, "session required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "agent not found")
        cwd = requested_root or str(agent.get("cwd") or "")
        if requested_root.startswith("@"):
            alias, _, suffix = requested_root.partition("/")
            if alias not in {"@workspace", "@home"}:
                return self._json_error(400, "unknown directory root")
            base = pathlib.Path(str(agent.get("cwd") or "")) if alias == "@workspace" else pathlib.Path.home()
            candidate = (base / suffix).resolve()
            try: candidate.relative_to(base.resolve())
            except ValueError:
                return self._json_error(403, "directory escapes root")
            cwd = str(candidate)
        if not cwd:
            return self._json_error(404, "agent workspace unavailable")
        try:
            root_candidate = pathlib.Path(cwd).expanduser()
            if requested_root and not root_candidate.is_absolute():
                return self._json_error(400, "root path must be absolute")
            # Explorer intentionally follows the authenticated operator trust
            # model of the terminal/agent CLI: any path readable by the server
            # user is browseable. It remains read-only and size-bounded.
            root = root_candidate
            if not root.is_dir():
                return self._json_error(404, "agent workspace unavailable")
            payload = list_directory(root, relative)
        except AgentFileError as exc:
            return self._json_error(exc.status, str(exc))
        except ValueError as exc:
            return self._json_error(403, str(exc))
        except (OSError, RuntimeError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok(payload)

    def _handle_agent_fork(self, session: str):
        """Fork an agent into helper agents that start from its conversation
        (``lib.agent_fork``); each child is sent its task from the parent."""
        from lib import agent_fork
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        refusal = self._bookkeeping_lineage_refusal(session)
        if refusal:
            return self._json_error(409, refusal)
        try:
            result = agent_fork.fork(self.ctx, session, data)
        except agent_fork.ForkError as e:
            log("agentForkRejected", f"{session} status={e.status} code={e.code} :: {e}")
            return self._json(e.status, e.response())
        return self._json_ok({"ok": True, **result})

    def _handle_agent_llm(self):
        """Set a running agent's model and/or reasoning effort. Read fresh on
        the agent's next turn, so this re-tunes a live agent without relaunch.
        Send "" for a field to clear the override (fall back to config default).
        Only fields present in the body are changed."""
        from lib import config
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        backend = backends.normalize(agent.get("backend"))
        valid = list(backends.valid_efforts(backend))
        update: dict[str, str] = {}
        if "model" in data:
            if data.get("model") is not None and not isinstance(data.get("model"), str):
                return self._json_error(400, "model must be string or null")
            model = (data.get("model") or "").strip()
            if not backends.is_valid_model(backend, model):
                return self._json(400, {
         "error": "invalid model for backend", "backend": backend,
     })
            update["model"] = model
        if "effort" in data:
            if data.get("effort") is not None and not isinstance(data.get("effort"), str):
                return self._json_error(400, "effort must be string or null")
            effort = (data.get("effort") or "").strip().lower()
            if effort and effort not in valid:
                return self._json(400, {"error": "invalid effort for backend",
                                        "backend": backend, "valid_efforts": valid})
            update["effort"] = effort
        next_model = update.get("model", str(agent.get("model") or "").strip())
        effort_compatibility_unknown = backends.by_id(backend).effort_compatibility_unknown
        if effort_compatibility_unknown and not next_model:
            next_model = backends.default_model_effort(backend, config.load())[0]
        next_effort = update.get("effort", str(agent.get("effort") or "").strip())
        if effort_compatibility_unknown and next_model and next_effort:
            return self._json(400, {
     "error": "AGY model-specific effort compatibility is unknown",
     "backend": backend,
 })
        if update:
            agents_db.update_agent(agent["agent_id"], **update)
        fresh = identity.lookup(session) or {}
        return self._json_ok({
            "ok": True, "backend": backend,
            "model": fresh.get("model", ""), "effort": fresh.get("effort", ""),
            "valid_efforts": valid,
        })

    def _handle_agent_mcp(self):
        """Set which MCP servers an agent loads (user-driven, from the app).
        Body: {"session": "...", "mcp_servers": ["name", ...]}. Names are
        validated against the global ~/.claude.json catalog; unknown names are
        dropped. Read fresh on the agent's next turn — no relaunch."""
        from lib import config
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        catalog = config.read_global_mcp_servers()
        requested = data.get("mcp_servers")
        if not isinstance(requested, list):
            return self._json_error(400, "mcp_servers must be a list")
        # Keep only known servers, de-duplicated, in request order.
        chosen: list[str] = []
        for name in requested:
            n = str(name).strip()
            if n in catalog and n not in chosen:
                chosen.append(n)
        from lib.mcp_selection import encode
        agents_db.update_agent(agent["agent_id"], mcp_servers=encode(chosen))
        return self._json_ok({
            "ok": True,
            "session": session,
            "mcp_servers": chosen,
            "available_mcp_servers": sorted(catalog.keys()),
        })

    def _handle_agent_model_options(self):
        from lib import provider_capabilities
        self._json_ok(provider_capabilities.endpoint_response())

    def _handle_agent_rename(self):
        """Change an agent's display name, keeping every stable identifier.

        The name is `agents.persona`. `session` and `agent_id` are untouched, so
        addressing, transcripts, pairings and routing all survive the rename.
        """
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        if self._reject_janitor_control(data):
            return
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        # Collapse internal runs of whitespace: this is a display name, and a
        # name that only differs by spacing is not a different contact.
        name = " ".join(str(data.get("name") or "").split())
        if not name or len(name) > 60:
            return self._json_error(400, "name required (maximum 60 characters)")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        # Agent creation permits one live session per contact name, and the
        # persona-to-session lookup relies on it. A rename has to hold the same
        # line or that lookup becomes ambiguous.
        owner = next((a for a in agents_db.list_agents()
                      if a["agent_id"] != agent["agent_id"]
                      and str(a["persona"] or "").strip().casefold() == name.casefold()),
                     None)
        if owner is not None:
            return self._json(409, {
     "error": "contact_occupied",
     "message": f"{owner['persona']} already has an active session.",
     "owner": owner["persona"], "session": owner["session"],
 })
        if str(agent.get("persona") or "") != name:
            agents_db.update_agent(agent["agent_id"], persona=name)
            events.broadcast(self.ctx.stream, events.agent_roster(
                "agent-renamed", session=session, name=name))
        return self._json_ok({
            "ok": True, "session": session, "name": name,
        })

    def _handle_create_agent(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        refusal = self._bookkeeping_lineage_refusal(str(data.get("parent") or ""))
        if refusal:
            return self._json_error(409, refusal)
        try:
            result = AgentLifecycleService(self.ctx).create(data)
        except AgentLifecycleError as e:
            # Log the rejection reason so a failed start is never invisible —
            # greppable as `agentCreateRejected` alongside the POST line.
            log("agentCreateRejected",
                f"status={e.status} code={e.code} :: {e.message}")
            return self._json(e.status, e.response())
        from lib.mcp_selection import decode as decode_mcp
        from lib.avatar_urls import versioned_avatar_url
        row = identity.lookup(result.session) or {}
        agent = {key: row.get(key) for key in (
            "agent_id", "session", "persona", "backend", "cwd", "model", "effort",
            "voice_id", "avatar_symbol", "muted", "heartbeat_enabled", "dreaming_enabled",
            "parent_agent_id", "role", "helper_state")}
        from lib.session_models import agent_model
        agent["model"] = agent_model(row, identity.backend_session(row))
        state = agents_db.latest_state(row["agent_id"]) or {}
        agent.update(alive=True, latest_state=state.get("kind") or "idle", last_activity=row.get("created_at", 0),
                     mcp_servers=decode_mcp(row.get("mcp_servers"))[1],
                     avatar_url=versioned_avatar_url("/avatars", row.get("agent_id", ""), row.get("avatar_path") or ""))
        return self._json_ok({
            "ok": True, "session": result.session, "name": result.persona, "agent": agent,
        })

    def _handle_delete_agent(self, session: str):
        from lib import turn_dispatch
        session = unquote(session).strip("/")
        agent = identity.lookup(session)
        if self._reject_janitor_control({"session": session}):
            return
        if agent and self._turn_caller() == agent["agent_id"]:
            # An agent deleting itself from its own turn: Koko, a copy of
            # Theo, deleted itself as a stray on 2026-10-10. Only the user
            # (the apps, or a shell without a turn) removes an agent's self.
            return self._json(403, {"error": "self_delete",
                                    "message": "an agent cannot delete itself from its own turn; "
                                               "ask the user"})
        if agent and getattr(self.ctx, "runtime_client", None) is None:
            # Invalidate dispatch state before delete interrupts the backend;
            # its terminal callback must not drain queued work after deletion.
            turn_dispatch.clear_for_agent(agent["agent_id"])
        try:
            AgentLifecycleService(self.ctx).delete(session)
        except AgentLifecycleError as e:
            return self._json(e.status, e.response())
        return self._json_ok({"ok": True})

    def _handle_prompt_history(self):
        """Authenticated history of prospectively evidenced user prompts."""
        if not (getattr(self.ctx, "auth_token", "") or ""):
            return self._json_error(503, "prompt history requires configured authentication")
        from lib.prompt_history import build_prompt_history

        qs = self._query()
        session_id = (qs.get("session_id", [""])[0] or "").strip()
        compatibility_session = (qs.get("session", [""])[0] or "").strip()
        if not session_id and not compatibility_session:
            return self._json_error(400, "session_id required")
        try:
            limit = int(qs.get("limit", ["50"])[0] or 50)
        except ValueError:
            return self._json_error(400, "invalid limit")
        before = (qs.get("before", [""])[0] or "").strip()
        try:
            history = build_prompt_history(
                session_id=session_id,
                compatibility_session_slug=compatibility_session,
                limit=limit,
                before=before,
            )
        except ValueError as exc:
            return self._json_error(400, str(exc))
        if history is None:
            return self._json_error(404, "session not found")
        self._send(
            200,
            json.dumps(history, ensure_ascii=False).encode(),
            "application/json",
        )

    def _handle_snapshot(self):
        """Unified per-agent read model for the dashboard.

        ``include_archived=0`` leaves the archived agents out and does not
        build them; GET /agents/archived pages those. Without it, or with any
        other value, the answer is every agent, as it has always been."""
        from lib import snapshot
        include_archived = self._query().get("include_archived", [""])[0] != "0"
        self._send(200, snapshot.SHARED.body(self.ctx, include_archived=include_archived),
                   "application/json")

    def _handle_archived_agents(self):
        """One page of the archived agents, as snapshot rows."""
        from lib import snapshot
        query = self._query()
        try:
            limit, position = snapshot.parse_archived_query(
                query.get("limit", [""])[0], query.get("cursor", [""])[0])
        except ValueError:
            # Only the query: a fault while building is the Host's, and is
            # answered and logged as the snapshot route's are.
            return self._json_error(400, "invalid archived agents query")
        self._json_ok(snapshot.SHARED.archived_page(self.ctx, limit=limit, position=position))
