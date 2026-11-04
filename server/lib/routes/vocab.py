"""VocabRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "vocab"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import sqlite3
from lib.log import log_exception
from lib.routes.helpers import _runtime_paths, _truthy_header


class VocabRoutes:
    def _handle_vocab_assignments_get(self):
        from lib import vocab_store
        self._json_ok({"assignments": vocab_store.assignments()})

    def _handle_vocab_assign_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        if not profile_id:
            return self._json_error(400, "profile_id is required")
        try:
            vocab_store.assign_profile(
                profile_id, agent_id=str(data.get("agent_id") or ""),
                team_id=str(data.get("team_id") or ""))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such profile")
        self._json_ok({"ok": True})

    def _handle_vocab_budgets_get(self):
        from lib import stt_providers
        from lib.vocab_compile import describe_budgets
        self._json_ok({"budgets": describe_budgets(),
                       "engine": stt_providers.selected_engine(),
                       "models": stt_providers.cloud_models()})

    def _handle_vocab_packs_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        vocab_store.delete_pack(pack_id)
        self._json_ok({"ok": True})

    def _handle_vocab_packs_get(self):
        from lib import vocab_store
        counts = vocab_store.pack_term_counts()
        packs = vocab_store.list_packs()
        for pack in packs:
            pack["terms"] = counts.get(pack["pack_id"], 0)
        self._json_ok({"packs": packs})

    def _handle_vocab_packs_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        if not name:
            return self._json_error(400, "name is required")
        kind = str(data.get("kind") or "static")
        if kind not in ("static", "dynamic"):
            return self._json_error(400, "kind must be static or dynamic")
        try:
            pack_id = vocab_store.create_pack(
                name, kind=kind, generator=str(data.get("generator") or ""),
                priority=float(data.get("priority", 1.0)),
                floor=int(data.get("floor", 0)),
                enabled=bool(data.get("enabled", True)))
        except sqlite3.IntegrityError:
            return self._json_error(409, "a pack with that name exists")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        for term in data.get("terms") or []:
            if isinstance(term, str):
                vocab_store.add_term(pack_id, term)
            elif isinstance(term, dict):
                vocab_store.add_term(
                    pack_id, str(term.get("text") or ""),
                    rarity=float(term.get("rarity", 0.5)),
                    say_as=str(term.get("say_as") or ""),
                    often_heard_as=str(term.get("often_heard_as") or ""))
        self._json_ok({"ok": True, "pack_id": pack_id})

    def _handle_vocab_packs_update(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        try:
            if "enabled" in data:
                vocab_store.set_pack_enabled(pack_id, bool(data["enabled"]))
            vocab_store.update_pack(
                pack_id, name=data.get("name"),
                priority=data.get("priority"), floor=data.get("floor"))
        except sqlite3.IntegrityError:
            return self._json_error(409, "a pack with that name exists")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True})

    def _handle_vocab_preview_get(self):
        query = self._query_first()
        preview = getattr(self.ctx, "vocab_preview", None)
        if not callable(preview):
            return self._json_error(503, "vocabulary preview unavailable")
        try:
            payload = preview(
                session=query.get("session", ""),
                requested_model=query.get("model", ""),
                delegated=_truthy_header(query.get("delegated", "")))
        except Exception as exc:  # noqa: BLE001
            log_exception("vocabPreviewFail", exc)
            return self._json_error(500, "vocabulary preview failed")
        self._json_ok(payload)

    def _handle_vocab_profile_get(self):
        from lib import vocab_store
        profile_id = self._query_first().get("profile_id", "")
        detail = vocab_store.profile_detail(profile_id) if profile_id else None
        if detail is None:
            return self._json_error(404, "no such profile")
        self._json_ok(detail)

    def _handle_vocab_profile_packs_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        pack_id = str(data.get("pack_id") or "")
        if not profile_id or not pack_id:
            return self._json_error(400, "profile_id and pack_id are required")
        try:
            vocab_store.add_pack_to_profile(
                profile_id, pack_id, int(data.get("position", 0)))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such profile or pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True})

    def _handle_vocab_profile_packs_remove(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        pack_id = str(data.get("pack_id") or "")
        if not profile_id or not pack_id:
            return self._json_error(400, "profile_id and pack_id are required")
        vocab_store.remove_pack_from_profile(profile_id, pack_id)
        self._json_ok({"ok": True})

    def _handle_vocab_profiles_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        if not profile_id:
            return self._json_error(400, "profile_id is required")
        vocab_store.delete_profile(profile_id)
        self._json_ok({"ok": True})

    def _handle_vocab_profiles_get(self):
        from lib import vocab_store
        self._json_ok({"profiles": vocab_store.list_profiles()})

    def _handle_vocab_profiles_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        if not name:
            return self._json_error(400, "name is required")
        try:
            profile_id = vocab_store.create_profile(name)
        except sqlite3.IntegrityError:
            return self._json_error(409, "a profile with that name exists")
        for position, pack_id in enumerate(data.get("pack_ids") or []):
            vocab_store.add_pack_to_profile(profile_id, str(pack_id), position)
        self._json_ok({"ok": True, "profile_id": profile_id})

    def _handle_vocab_profile_workspace(self):
        """Turn workspace harvesting on or off for one profile.

        Off is the default and the safe answer: reading an agent's folders is
        a choice, and the absence of one is what let a package tree write most
        of a biasing payload.
        """
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "").strip()
        if not profile_id:
            return self._json_error(400, "profile_id is required")
        if not isinstance(data.get("enabled"), bool):
            return self._json_error(400, "enabled must be a boolean")
        if vocab_store.profile_detail(profile_id) is None:
            return self._json_error(404, "no such profile")
        vocab_store.set_profile_harvests_workspace(profile_id, data["enabled"])
        self._json_ok({
            "ok": True, "profile_id": profile_id,
            "harvests_workspace": vocab_store.profile_harvests_workspace(profile_id),
        })

    def _handle_vocab_run_get(self):
        from lib import heard_audio, vocab_store
        trace_id = self._query_first().get("trace_id", "")
        run = vocab_store.run_for_trace(trace_id) if trace_id else None
        if run is None:
            return self._json_error(404, "no run for that trace")
        kept = heard_audio.lookup(
            _runtime_paths().cache_dir, trace_id)
        run["audio_url"] = f"/transcription-audio?trace_id={trace_id}" if kept else None
        self._json_ok(run)

    def _handle_vocab_runs_get(self):
        from lib import vocab_store
        query = self._query_first()
        try:
            limit = max(1, min(200, int(query.get("limit") or 20)))
        except ValueError:
            limit = 20
        self._json_ok({"runs": vocab_store.recent_runs(
            limit, session=query.get("session", ""))})

    def _handle_vocab_terms_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        try:
            term_id = int(data.get("term_id"))
        except (TypeError, ValueError):
            return self._json_error(400, "term_id is required")
        vocab_store.delete_term(term_id)
        self._json_ok({"ok": True})

    def _handle_vocab_terms_get(self):
        from lib import vocab_store
        pack_id = self._query_first().get("pack_id", "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        self._json_ok({"pack_id": pack_id, "terms": vocab_store.list_terms(pack_id)})

    def _handle_vocab_terms_post(self):
        from lib import vocab_store
        from lib.vocab_generators import estimate_rarity
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        text = str(data.get("text") or "").strip()
        if not pack_id or not text:
            return self._json_error(400, "pack_id and text are required")
        rarity = data.get("rarity")
        try:
            added = vocab_store.add_term(
                pack_id, text,
                rarity=float(rarity) if rarity is not None else estimate_rarity(text),
                say_as=str(data.get("say_as") or ""),
                often_heard_as=str(data.get("often_heard_as") or ""),
                source=str(data.get("source") or "manual"))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True, "added": added})

    def _handle_vocab_terms_update(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        try:
            term_id = int(data.get("term_id"))
        except (TypeError, ValueError):
            return self._json_error(400, "term_id is required")
        try:
            updated = vocab_store.update_term(
                term_id, text=data.get("text"), say_as=data.get("say_as"),
                often_heard_as=data.get("often_heard_as"), rarity=data.get("rarity"))
        except sqlite3.IntegrityError:
            return self._json_error(409, "that term already exists in the pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True, "updated": updated})

    def _handle_vocab_unassign_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        agent_id = str(data.get("agent_id") or "")
        team_id = str(data.get("team_id") or "")
        if not agent_id and not team_id:
            return self._json_error(400, "agent_id or team_id is required")
        vocab_store.unassign(agent_id=agent_id, team_id=team_id)
        self._json_ok({"ok": True})

    def _vocab_body(self) -> dict | None:
        data = self._read_json()
        if not isinstance(data, dict):
            self._json_error(400, "bad json")
            return None
        return data
