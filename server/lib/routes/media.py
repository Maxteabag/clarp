"""MediaRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "media"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import secrets
from urllib.parse import unquote
from lib import eventlog
from lib import events
from lib import media_store
from lib.log import log_exception
from lib.paths import _safe_session
from lib.focus import current_focus_session
from lib.routes.helpers import _runtime_paths, _safe_upload_name


class MediaRoutes:
    def _handle_media_list(self):
        qs = self._query()
        session = (qs.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        favorites = (qs.get("favorites", [""])[0] or "").strip() in {"1", "true"}
        try:
            page = media_store.list_page(session, limit=limit, favorites=favorites,
                                         before=qs.get("before", [""])[0] or "")
        except media_store.MediaError as e:
            return self._json_error(e.status, str(e))
        self._json_ok({"session": session, **page})

    def _handle_media_deleted(self):
        """GET /media/deleted?since=<ms>[&session=][&limit=]: images deleted since, for a
        device that was away (contract 489, section 3b). Whoever may read GET /media may."""
        qs = self._query()
        try:
            since = int((qs.get("since", [""])[0] or "").strip())
        except ValueError:
            since = -1
        if since < 0:
            return self._json_error(400, "since must be a time in ms")
        try:
            limit = int((qs.get("limit", ["1000"])[0] or "1000").strip())
        except ValueError:
            limit = 0
        if not 1 <= limit <= 1000:
            return self._json_error(400, "limit must be a number from 1 to 1000")
        session = (qs.get("session", [""])[0] or "").strip()
        self._json_ok(media_store.deleted_since(since=since, session=session, limit=limit))

    # ---- delete and favourite a gallery image (feature media_manage) ----

    def _media_write_refused(self) -> bool:
        """An agent may not delete or mark images: a request carrying a turn token is
        refused, whoever's. A limited device was refused before dispatch."""
        if (self.headers.get("X-Clarp-Turn") or "").strip():
            self._json(403, {"error": "agents_may_not"})
            return True
        return False

    def _media_changed(self, row, change, at, **extra):
        events.broadcast(self.ctx.stream, events.media_changed(
            session=row["session"], agent_id=row["agent_id"], asset_id=row["asset_id"],
            change=change, at=at, **extra))

    def _media_delete_one(self, asset_id):
        """(the answer entry, the HTTP status for a single delete)."""
        deleted_by = self.principal().principal or None
        try:
            answer, row, changed = media_store.delete(asset_id, deleted_by=deleted_by)
        except media_store.MediaError as e:
            return {"asset_id": asset_id, "deleted": False, "error": str(e)}, e.status
        if changed:
            eventlog.emit("server", "mediaDelete", session=row["session"],
                          detail={"asset_id": asset_id, "by": deleted_by})
            self._media_changed(row, "deleted", row["deleted_at"])
        return answer, 200

    def _handle_media_delete(self, asset_id):
        if self._media_write_refused():
            return
        answer, status = self._media_delete_one(asset_id)
        if status != 200:
            return self._json_error(status, answer["error"])
        self._json_ok(answer)

    def _handle_media_delete_batch(self):
        if self._media_write_refused():
            return
        data = self._read_json()
        ids = (data or {}).get("asset_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200 \
                or not all(isinstance(item, str) and item for item in ids):
            return self._json_error(400, "asset_ids must list 1 to 200 asset ids")
        self._json_ok({"results": [self._media_delete_one(item)[0] for item in ids]})

    def _handle_media_favorite(self, asset_id):
        if self._media_write_refused():
            return
        data = self._read_json()
        favorite = (data or {}).get("favorite")
        if not isinstance(favorite, bool):
            return self._json_error(400, "favorite must be true or false")
        try:
            asset, row, changed_at = media_store.set_favorite(asset_id, favorite)
        except media_store.Removed as e:
            return self._json(410, {"error": "removed", "asset_id": asset_id,
                                    "deleted_at": e.row["deleted_at"]})
        except media_store.MediaError as e:
            return self._json_error(e.status, str(e))
        if changed_at is not None:
            self._media_changed(row, "favorite", changed_at, favorite=favorite)
        self._json_ok(asset)

    def _handle_media_publish(self):
        """Publish an agent-produced image or allowlisted artifact file.

        Agents call this via the clarp-media helper. The server performs the
        two-phase work (blob copy + SQLite row) so agents never have to update
        both a directory and the DB themselves.
        """
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > media_store.MAX_MEDIA_BYTES:
                return self._json_error(400, "bad size")
            blob = self.rfile.read(n)
            self._body_consumed = True
            if len(blob) != n:
                return self._json_error(400, "incomplete body")
        except (ValueError, OSError) as e:
            log_exception("mediaReadFail", e)
            return self._json_error(400, str(e))

        session = (self.headers.get("X-Session") or "").strip()
        if not session:
            session = self.ctx.default_session
        name = unquote((self.headers.get("X-File-Name") or "").strip())
        caption = unquote((self.headers.get("X-Caption") or "").strip())
        created_by = (self.headers.get("X-Created-By") or "agent").strip()
        try:
            asset = media_store.publish(
                session=session,
                blob=blob,
                source_name=name,
                content_type=self.headers.get("Content-Type", ""),
                caption=caption,
                created_by=created_by,
                media_dir=self.ctx.media_dir or _runtime_paths().media_dir,
            )
        except media_store.MediaError as e:
            return self._json_error(e.status, str(e))
        except OSError as e:
            log_exception("mediaWriteFail", e)
            return self._json_error(500, str(e))

        eventlog.emit("server", "mediaPublish", session=session or None,
                      detail={"asset_id": asset["asset_id"],
                              "name": asset["source_name"],
                              "size": asset["bytes"]})
        if asset["markdown"]:  # an image: the chat's gallery has a new picture
            self._media_changed(media_store.lookup(asset["asset_id"]), "created",
                                asset["created_at"])
        body = json.dumps({"ok": True, "asset": asset, **asset}).encode()
        self._send(200, body, "application/json")

    def _handle_upload(self):
        """Receive a raw file body from a client (e.g. an image picked on the
        phone) and save it under this server's per-session uploads dir. Returns
        the absolute path so the client can drop it into the prompt — agents
        run with --dangerously-skip-permissions, so they read it directly."""
        from lib import upload_results
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 50 * 1024 * 1024:  # cap at 50 MB per upload
                return self._json_error(400, "bad size")
            blob = self.rfile.read(n)
            self._body_consumed = True
        except (ValueError, OSError) as e:
            log_exception("uploadReadFail", e)
            return self._json_error(400, str(e))
        if len(blob) != n:
            self.close_connection = True
            return self._json_error(408, "incomplete upload")

        # Which agent/session this attaches to: explicit header, then the
        # focused session, then the default — same precedence the rest of the
        # API uses.
        session = (self.headers.get("X-Session") or "").strip()
        if not session:
            session = current_focus_session()
        if not session:
            session = self.ctx.default_session

        name = _safe_upload_name(
            unquote((self.headers.get("X-File-Name") or "").strip()),
            self.headers.get("Content-Type", ""),
        )
        base = self.ctx.uploads_dir or _runtime_paths().uploads_dir
        dest_dir = base / _safe_session(session)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            upload_id = (self.headers.get("X-Upload-ID") or "").strip()
            if upload_id:
                dest = upload_results.store(
                    session_dir=dest_dir, upload_id=upload_id, name=name,
                    content_type=self.headers.get("Content-Type", ""), blob=blob,
                    record_root=base)
            else:
                # No idempotency key: every request stores a new file.
                dest = dest_dir / f"{secrets.token_hex(4)}-{name}"
                dest.write_bytes(blob)
        except upload_results.UploadIDCollisionError as e:
            return self._json_error(409, str(e))
        except ValueError as e:
            return self._json_error(400, str(e))
        except OSError as e:
            log_exception("uploadWriteFail", e)
            return self._json_error(500, str(e))

        eventlog.emit("server", "upload", session=session or None,
                      detail={"name": name, "size": len(blob), "path": str(dest)})
        body = json.dumps({"ok": True, "path": str(dest), "name": name,
                           "size": len(blob), "session": session}).encode()
        self._send(200, body, "application/json")
