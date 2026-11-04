"""MediaRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "media"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import secrets
from urllib.parse import unquote
from lib import eventlog
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
        assets = media_store.list_for_session(session, limit=limit)
        self._json_ok({"session": session, "assets": assets})

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
