"""TeamRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "teams"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations


class TeamRoutes:
    def _handle_team_add_member(self, team_id: str):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        agent_id = str(data.get("agent_id") or "").strip()
        if not agent_id:
            return self._json_error(400, "agent_id required")
        if not team_store.add_member(team_id, agent_id):
            return self._json_error(404, "team or agent not found")
        return self._json_ok({
            "ok": True,
            "team": team_store.get_team(team_id),
        })

    def _handle_team_create(self):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            team = team_store.create_team(
                str(data.get("name") or ""),
                color=str(data.get("color") or ""),
                parent_team_id=data.get("parent_team_id"),
            )
        except ValueError as e:
            return self._json_error(400, str(e))
        return self._json_ok({"ok": True, "team": team})

    def _handle_team_delete(self, team_id: str):
        from lib import team_store
        if not team_id:
            return self._json_error(404, "team not found")
        if not team_store.delete_team(team_id):
            return self._json_error(404, "team not found")
        return self._json_ok({"ok": True})

    def _handle_team_messages(self, team_id: str):
        from lib import team_store
        if not team_id:
            return self._json_error(404, "team not found")
        qs = self._query()
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        body = {
            "team_id": team_id,
            "messages": team_store.list_team_messages(team_id, limit=limit),
        }
        return self._json_ok(body)

    def _handle_team_nudging(self):
        """Enable/disable autonomous leader nudging for a team."""
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        team_id = str(data.get("team_id") or "").strip()
        if not team_id:
            return self._json_error(400, "team_id required")
        if "nudge_enabled" not in data:
            return self._json_error(400, "nudge_enabled required")
        team = team_store.set_nudge_enabled(team_id, data.get("nudge_enabled") is True)
        if team is None:
            return self._json_error(404, "team not found")
        return self._json_ok({"ok": True, "team": team})

    def _handle_team_remove_member(self, team_id: str, agent_id: str):
        from lib import team_store
        if not team_id or not agent_id:
            return self._json_error(404, "team or agent not found")
        team_store.remove_member(team_id, agent_id)
        return self._json_ok({
            "ok": True,
            "team": team_store.get_team(team_id),
        })

    def _handle_team_update(self, team_id: str):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            team = team_store.update_team(team_id, **{
                key: data[key] for key in (
                    "name", "color", "archived", "parent_team_id", "leader",
                    "leader_enabled", "communication_enabled"
                ) if key in data
            })
        except ValueError as e:
            return self._json_error(400, str(e))
        if team is None:
            return self._json_error(404, "team not found")
        return self._json_ok({"ok": True, "team": team})

    def _handle_teams_list(self):
        from lib import team_store
        body = {"teams": team_store.list_teams()}
        return self._json_ok(body)
