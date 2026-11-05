"""HeartbeatRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "heartbeat"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from lib import db
from lib import agents as agents_db
from lib import backends
from lib import identity
from lib.log import log_exception


class HeartbeatRoutes:
    def _dreaming_backend_options(self):
        """Backends the dreaming picker may offer, with their effort levels.

        Model lists are deliberately not duplicated here — a client already
        fetches `/agent-model-options` for the full catalogue and can filter it
        by the chosen backend id.
        """
        from lib import provider_capabilities
        # Model ids come from the same catalogue the agent picker uses, so the
        # dreaming picker can never drift from what the CLIs actually accept.
        # A probe failure degrades to backends-only rather than failing the
        # settings read.
        models: dict[str, list[dict[str, str]]] = {}
        try:
            catalog = provider_capabilities.capability_catalog()
            providers = catalog.get("providers") or {}
            rows = (providers.values() if isinstance(providers, dict)
                    else providers)
            for row in rows:
                provider_id = str(row.get("id") or "")
                if not provider_id:
                    continue
                models[provider_id] = [
                    {
                        "id": str(m.get("id") or m.get("slug") or ""),
                        "label": str(m.get("label") or m.get("display_name")
                                     or m.get("id") or m.get("slug") or ""),
                    }
                    for m in (row.get("models") or [])
                    if (m.get("id") or m.get("slug"))
                ]
        except Exception as e:  # noqa: BLE001
            log_exception("dreamingBackendOptionsModelsFail", e)
        options = [{
            "id": "",
            "label": "Same as the agent",
            "efforts": [],
            "models": [],
        }]
        for backend_id in backends.ids():
            fields = backends.catalogue_fields(backend_id)
            options.append({
                "id": backend_id,
                "label": fields.get("label") or backend_id,
                "efforts": list(backends.valid_efforts(backend_id)),
                "models": models.get(backend_id, []),
            })
        return options

    def _handle_agent_dreaming(self):
        """Enable/disable nightly creative dreaming for a single live agent."""
        from lib import dreaming
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        if "dreaming_enabled" not in data:
            return self._json_error(400, "dreaming_enabled required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        enabled = data.get("dreaming_enabled") is True
        agents_db.update_agent(agent["agent_id"], dreaming_enabled=enabled)
        fresh = identity.lookup(session) or {}
        settings = dreaming.get_settings()
        return self._json_ok({
            "ok": True,
            "session": session,
            "dreaming_enabled": bool(fresh.get("dreaming_enabled")),
            "dream_target_hour": dreaming.DREAM_TARGET_HOUR,
            "dream_planned_rounds": settings.planned_rounds,
            "dream_target_tokens": settings.target_token_budget,
            "dream_min_directions": settings.min_directions,
            "dream_settings": settings.as_dict(),
            "dream_prompt": dreaming.dreaming_prompt_text(),
        })

    def _handle_agent_heartbeat(self):
        """Enable/disable autonomous heartbeat for a single live agent."""
        from lib import heartbeat
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        if "heartbeat_enabled" not in data:
            return self._json_error(400, "heartbeat_enabled required")
        if not isinstance(data.get("heartbeat_enabled"), bool):
            return self._json_error(400, "heartbeat_enabled must be boolean")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        enabled = bool(data["heartbeat_enabled"])
        agents_db.update_agent(
            agent["agent_id"],
            heartbeat_enabled=enabled,
        )
        heartbeat.record_heartbeat_activity(agent["agent_id"])
        fresh = identity.lookup(session) or {}
        return self._json_ok({
            "ok": True,
            "session": session,
            "heartbeat_enabled": bool(fresh.get("heartbeat_enabled")),
        })

    def _handle_agent_heartbeat_status(self):
        """Return one Agent's scheduler projection and recent heartbeat outcomes."""
        from lib import heartbeat
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        rows = db.conn().execute(
            """SELECT message_id, text, timestamp, updated_at
                 FROM messages
                WHERE agent_id=? AND role='assistant' AND origin='heartbeat'
                ORDER BY updated_at DESC LIMIT 20""",
            (agent["agent_id"],),
        ).fetchall()
        history = [{
            "id": row["message_id"], "text": row["text"],
            "timestamp": row["timestamp"], "updated_at": row["updated_at"],
        } for row in rows]
        return self._json_ok({
            "ok": True, "session": session,
            "schedule": heartbeat.agent_schedule(agent), "history": history,
        })

    def _handle_dreaming_run_post(self):
        """Start one dream immediately, with an optional pinned recipe."""
        from lib import dreaming
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        session = str(data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        try:
            run = dreaming.start_manual_run(
                session,
                seed_strategy=str(data.get("seed_strategy") or "").strip(),
                context_dose=str(data.get("context_dose") or "").strip(),
            )
        except ValueError as e:
            return self._json_error(409, str(e))
        return self._json_ok({
            "ok": True,
            "run_id": run.get("run_id"),
            "session": run.get("session"),
            "seed_strategy": run.get("seed_strategy"),
            "context_dose": run.get("context_dose"),
            "seed_material": run.get("seed_material"),
        })

    def _handle_dreaming_runs(self):
        """Inspect recent deep dreaming run/thread/round ledger entries."""
        from lib import dreaming
        qs = self._query()
        session = (qs.get("session", [""])[0] or "").strip()
        try:
            limit = int(qs.get("limit", ["10"])[0] or "10")
        except ValueError:
            limit = 10
        runs = dreaming.list_dream_runs(session=session, limit=limit)
        settings = dreaming.get_settings()
        return self._json_ok({
            "ok": True,
            "runs": runs,
            "contract": settings.as_dict(),
        })

    def _handle_dreaming_settings_get(self):
        from lib import dream_seeds, dreaming
        return self._json_ok({
            "ok": True,
            "settings": dreaming.get_settings().as_dict(),
            "limits": {
   "dreams_per_night": [
       dreaming.DREAMS_PER_NIGHT_MIN,
       dreaming.DREAMS_PER_NIGHT_MAX,
   ],
   "direction_count": [
       dreaming.DREAM_DIRECTION_MIN,
       dreaming.DREAM_DIRECTION_MAX,
   ],
   "target_token_budget": [
       dreaming.DREAM_TOKEN_BUDGET_MIN,
       dreaming.DREAM_TOKEN_BUDGET_MAX,
   ],
            },
            # Everything a client needs to draw the dreaming backend/model/
            # effort pickers without shipping its own catalogue. An empty
            # choice means "inherit the agent's own backend".
            "options": {
   "backends": self._dreaming_backend_options(),
   "seed_strategies": list(dream_seeds.STRATEGIES),
   "context_doses": list(dream_seeds.CONTEXT_DOSES),
            },
        })

    def _handle_dreaming_settings_put(self):  # noqa: D401
        from lib import dreaming
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        settings = dreaming.update_settings(data)
        return self._json_ok({
            "ok": True,
            "settings": settings.as_dict(),
        })

    def _handle_heartbeat_settings_get(self):
        """Return the heartbeat policy owned by this Computer."""
        from lib import heartbeat
        return self._json_ok({
            "ok": True,
            "settings": heartbeat.get_settings().as_dict(),
            "heartbeat_prompt": heartbeat.HEARTBEAT_PROMPT,
        })

    def _handle_heartbeat_settings_post(self):
        """Atomically update the heartbeat policy for this Computer."""
        from lib import heartbeat
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            settings = heartbeat.update_settings(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        return self._json_ok({
            "ok": True,
            "settings": settings.as_dict(),
            "heartbeat_prompt": heartbeat.HEARTBEAT_PROMPT,
        })
