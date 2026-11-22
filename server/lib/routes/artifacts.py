"""ArtifactRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "artifacts"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import sqlite3
import threading
from lib import events


def _can_render(handler):
    """The highest render_spec catalog version the requesting client draws (0: none)."""
    from lib import config, server_identity
    loaded = config.load()
    return server_identity.render_spec_level(handler.headers.get(server_identity.CLIENT_HEADER),
                                             loaded.render_spec_min_contract, loaded.render_spec_v2_min_contract)


def _administrator(handler):
    """The local administrator's token (or a Host with no token, from this machine), never a
    paired device: the same class controller actions use."""
    if getattr(handler, "_request_principal", "") == "administrator":
        return True
    return (not (getattr(handler.ctx, "auth_token", "") or "")
            and bool(getattr(handler, "_request_is_local", False)))


def _spec_refusal(handler, error):
    from lib.log import log
    triple = error.triple()  # the catalog_unavailable triple whenever the catalog cannot be read
    code = triple["code"]
    log("renderSpecRefused", f"code={code} path={triple['path']}")
    status = (503 if code == "catalog_unavailable" else 403 if code == "client_contract" else 409 if code in
              {"reused_id", "reused_id_audio", "version_mismatch", "already_answered", "not_open"} else 400)
    return handler._json(status, {"error": triple})


class ArtifactRoutes:
    def _artifact_mutation_row(self, row):
        if row["type"] == "render_spec":
            from lib import render_spec_store
            return render_spec_store.summary(row, _can_render(self))
        return row

    def _handle_render_spec_catalog(self):
        return self._send_render_spec_catalog(1)

    def _handle_render_spec_catalog_v2(self):
        return self._send_render_spec_catalog(2)

    def _send_render_spec_catalog(self, version):
        from lib import render_spec
        try:
            raw = render_spec.catalog(version).raw
        except render_spec.Refusal as exc:
            return _spec_refusal(self, exc)
        return self._send(200, raw, "application/json")

    def _broadcast_artifact(self, artifact: dict) -> None:
        if getattr(self.ctx, "stream", None) is None:
            return
        events.broadcast(self.ctx.stream, events.artifact_updated(
            session=artifact.get("session", ""),
            agent_id=artifact.get("agent_id", ""),
            artifact_id=artifact.get("artifact_id", "")))
        from lib import artifacts
        attention_count = len(artifacts.attention(include_questions=True))
        events.broadcast(self.ctx.stream, events.attention_updated(
            attention_count=attention_count))
        pusher = getattr(self.server, "live_activity_pusher", None)
        if pusher is not None:
            from lib import live_activity
            threading.Thread(target=lambda: pusher.set_attention(live_activity.badge_attention()),
                             daemon=True, name="live-activity-attention").start()

    def _handle_artifact_archive(self, artifact_id: str):
        return self._handle_artifact_inbox_action(artifact_id, discard=False)

    def _publishing_trace(self, session: str) -> str:
        """The trace an artifact is published under (R102): the trace of the turn whose token the
        request carries, only when that turn belongs to the artifact's own agent and is that
        agent's running turn now. Anything else, a job its turn started publishing after the turn
        was completed, stopped or closed included, gets '' and the apps place the card by time. Nothing the caller writes is
        taken: the token is the Host's own proof. A lookup that fails is the retryable 503 the
        turn identity gives (HTTP-N5)."""
        from lib import agents, turn_identity
        from lib.provider_background_jobs import TURN_ENV
        token = (getattr(self, "headers", None) or {}).get("X-Clarp-Turn", "")
        if not token:
            return ""
        try:
            agent_id, trace = turn_identity.caller_turn({TURN_ENV: token})
        except Exception as exc:  # noqa: BLE001 - fail closed: refused, logged and retryable
            from lib.log import log_exception
            log_exception("turnCallerLookupFail", exc)
            raise turn_identity.TurnIdentityUnavailable() from exc
        owner = agents.get_by_session(session.strip()) if session.strip() else None
        if not trace or not owner or owner["agent_id"] != agent_id:
            return ""
        return trace if turn_identity.running_trace(agent_id, trace) else ""

    def _handle_artifact_create(self):
        from lib import artifacts, render_spec
        data = self._read_json()
        if not isinstance(data, dict): return self._json_error(400, "json object required")
        if str(data.get("type") or "").strip().lower() in {"decision", "question", "plan"}:
            return self._json_error(409, "reserved artifact type")
        try:
            validate_only = self._query().get("validate_only") == ["1"]
            if str(data.get("type") or "").strip().lower() == "render_spec":
                render_spec.unique_object(data)
            row = artifacts.create(
                session=str(data.get("session") or ""), type=str(data.get("type") or ""),
                title=str(data.get("title") or ""), summary=str(data.get("summary") or ""),
                status=str(data.get("status") or "ready"),
                reference_id=str(data.get("reference_id") or ""), payload=data.get("payload"),
                artifact_id=str(data.get("artifact_id") or ""), expected_version=data.get("expected_version"),
                validate_only=validate_only,
                source_trace_id=self._publishing_trace(str(data.get("session") or "")))
        except render_spec.Refusal as exc:
            return _spec_refusal(self, exc)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return self._json_error(409, str(exc))
        if validate_only:
            return self._json_ok(row)
        self._broadcast_artifact(row)
        return self._json(201, {"artifact": self._artifact_mutation_row(row)})

    def _handle_artifact_discard(self, artifact_id: str):
        return self._handle_artifact_inbox_action(artifact_id, discard=True)

    def _handle_artifact_events_config(self, artifact_id: str, *, write: bool = True):
        if getattr(self, "_request_device_scope", "") == "limited":
            return self._json_error(403, "full device access required")
        from lib import html_form_events
        try:
            return self._json_ok(html_form_events.configure(artifact_id, self._read_json()) if write
                                 else html_form_events.configuration(artifact_id))
        except (ValueError, TypeError) as exc:
            return self._json_error(400, str(exc))

    def _handle_artifact_export(self, artifact_id: str):
        """GET /artifacts/<id>/export (R137): a render_spec as a move to another Host needs it, for
        the administrator only: any device is refused, limited or full, whatever its
        X-Clarp-Client says. Another kind is refused in words, in a body shaped so a kind can be
        added later: a 'kind' and one object under the kind's name."""
        from lib import render_spec_store
        # The administrator's token from the Authorization header only: never one a link carries in
        # its query string, nor a browser's cookie, for a route that hands out a user's answers.
        if not _administrator(self) or getattr(self, "_request_auth_source", "") not in ("header", ""):
            return self._json_error(403, "administrator only")
        try:
            exported = render_spec_store.export(artifact_id)
        except render_spec_store.NotExportable as kind:
            return self._json(409, {"error": f"export is not available for {kind} artifacts",
                                    "kind": str(kind)})
        if exported is None:
            return self._json_error(404, "not found")
        return self._json_ok(exported)

    def _handle_artifact_get(self, artifact_id: str):
        from lib import artifacts
        row = artifacts.get(artifact_id)
        if not row:
            return self._json_error(404, "artifact not found")
        if row["type"] == "render_spec":
            from lib import render_spec, render_spec_store
            if render_spec_store.drawable(row, _can_render(self)):
                try:
                    render_spec.catalog(row.meta["catalog_version"])
                except render_spec.Refusal as exc:
                    return _spec_refusal(self, exc)
                row = render_spec_store.open_current(artifact_id)
            return self._json_ok({"artifact": row})
        # Exactly flat-v1 drops the duplicate `payload`; any other value is
        # ignored, as it always was (no new 400 on this route).
        if self._query().get("representation", [""])[0] == "flat-v1":
            row = artifacts.response_representation(row, "flat-v1")
        return self._json_ok({"artifact": row})

    def _handle_artifact_html(self, artifact_id: str):
        if getattr(self, "_request_device_scope", "") == "limited":
            return self._json_error(403, "full device access required")
        # The page's own scripts can read document.URL and may fetch declared
        # connect_origins, so a ?token= credential would leak to them.
        if getattr(self, "_request_auth_source", "") == "query":
            return self._json_error(403, "send the credential in a header or cookie, not the URL")
        from lib import artifacts, html_forms
        row = artifacts.get(artifact_id)
        if not row or row["type"] != "html_form":
            return self._json_error(404, "HTML artifact not found")
        body, policy = html_forms.render_html(row["payload"])
        return self._send(200, body, "text/html; charset=utf-8", {
            "Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer"})

    def _handle_artifact_inbox_action(self, artifact_id: str, *, discard: bool):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "json object required")
        updated_at = data.get("expected_updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int):
            return self._json_error(400, "expected_updated_at must be an integer")
        if not discard and not isinstance(data.get("archived"), bool):
            return self._json_error(400, "archived must be a boolean")
        try:
            if discard:
                row, changed = artifacts.discard(artifact_id, expected_updated_at=updated_at)
            else:
                row, changed = artifacts.archive(
                    artifact_id, archived=data["archived"], expected_updated_at=updated_at)
        except ValueError as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        return self._json_ok({"artifact": self._artifact_mutation_row(row), "changed": changed})

    def _handle_artifact_pin(self, artifact_id: str):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "json object required")
        if not isinstance(data.get("pinned"), bool):
            return self._json_error(400, "pinned must be a boolean")
        try:
            row, changed = artifacts.pin(artifact_id, pinned=data["pinned"])
        except ValueError as exc:
            status = 404 if str(exc) == "artifact not found" else 409
            return self._json_error(status, str(exc))
        if changed:
            self._broadcast_artifact(row)
        return self._json_ok({"artifact": self._artifact_mutation_row(row), "changed": changed})

    def _handle_artifact_submit(self, artifact_id: str):
        from lib import html_forms, render_spec
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "JSON object required")
        try:
            receipt = html_forms.submit(artifact_id, data, render_capable=_can_render(self),
                                        on_completed=self._broadcast_artifact)
        except render_spec.Refusal as exc:
            return _spec_refusal(self, exc)
        except html_forms.ReadOnlyForm as exc:
            return self._json_error(400, str(exc))
        except ValueError as exc:
            return self._json_error(409, str(exc))
        return self._json_ok(receipt)

    def _handle_artifact_update(self, artifact_id: str):
        from lib import artifacts, render_spec
        data = self._read_json()
        if not isinstance(data, dict): return self._json_error(400, "json object required")
        try: row = artifacts.update(artifact_id, data)
        except render_spec.Refusal as exc:
            return _spec_refusal(self, exc)
        except ValueError as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        return self._json_ok({"artifact": self._artifact_mutation_row(row)})

    def _handle_artifacts_list(self):
        from lib import artifacts
        qs = self._query()
        representation = qs.get("representation", [""])[0]
        if representation not in artifacts.REPRESENTATIONS:
            return self._json_error(400, "unsupported artifact representation")
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
            offset = int(qs.get("offset", ["0"])[0] or 0)
            created_from = int(qs["created_from"][0]) if qs.get("created_from") else None
            created_to = int(qs["created_to"][0]) if qs.get("created_to") else None
        except (TypeError, ValueError):
            return self._json_error(400, "invalid limit")
        rows = artifacts.list_artifacts(
            session=(qs.get("session", [""])[0] or "").strip(),
            agent_id=(qs.get("agent_id", [""])[0] or "").strip(),
            type=(qs.get("type", [""])[0] or "").strip(),
            search=(qs.get("search", [""])[0] or "").strip(),
            created_from=created_from, created_to=created_to,
            limit=limit, offset=offset,
            order=(qs.get("order", ["updated"])[0] or "updated").strip(),
        )
        with artifacts.summary_request() as budget:
            body = {"artifacts": [
                artifacts.response_representation(row, representation, budget=budget,
                                                  render_capable=row.get("type") == "render_spec" and _can_render(self))
                for row in rows]}
        return self._json_ok(body)

    def _handle_attention(self):
        from lib import artifacts, janitor_attention
        query = self._query()
        items = artifacts.attention(
            include_questions=query.get("decision_format", [""])[0] == "2",
            include_archived=query.get("include_archived", [""])[0] == "1")
        self._deliver_pending_decisions()
        # An explicit format advertisement lets helpers reject older Hosts
        # which otherwise silently interpret question creation as approval.
        return self._json_ok({
            "items": items, "count": len(items), "decision_format": 2,
            "janitor_items": janitor_attention.pending(
   include_archived=query.get("include_archived", [""])[0] == "1"),
        })

    def _handle_attention_inbox(self):
        from lib import attention_index
        query = self._query()
        try:
            result = attention_index.page(limit=int(query.get("limit", ["100"])[0]),
                                          cursor=query.get("cursor", [""])[0],
                                          representation=query.get("representation", [""])[0], render_capable=_can_render(self))
        except attention_index.StaleCursor as exc:
            return self._json_error(409, str(exc))
        except (ValueError, TypeError):
            return self._json_error(400, "invalid attention query")
        return self._json_ok(result)

    def _handle_decision_create(self):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict): return self._json_error(400, "json object required")
        try:
            row = artifacts.create_decision(
                session=str(data.get("session") or ""), title=str(data.get("title") or "Decision"),
                question=str(data.get("question") or ""), context=str(data.get("context") or ""),
                yes_label=str(data.get("yes_label") or "Yes"), no_label=str(data.get("no_label") or "No"),
                payload=data.get("payload"), reference_id=str(data.get("reference_id") or ""),
                expires_at=data.get("expires_at"),
                response_type=data.get("response_type", "approval"),
                options=data.get("options"), allow_custom_text=data.get("allow_custom_text"),
                recommended_option_id=data.get("recommended_option_id"),
                blocks_progress=data.get("blocks_progress", False),
                priority_reason=data.get("priority_reason", ""),
                urgency=data.get("urgency", "normal"),
                response_effort=data.get("response_effort", "review"),
                deadline_at=data.get("deadline_at"),
                input_hint=data.get("input_hint"))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        return self._json(201, {"artifact": row})

    def _handle_decision_dismiss(self, decision_id: str):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "json object required")
        revision = data.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return self._json_error(400, "expected_revision must be an integer")
        try:
            row, changed = artifacts.dismiss(decision_id, expected_revision=revision)
        except ValueError as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        self._deliver_pending_decisions()
        return self._json_ok({
            "artifact": row, "changed": changed,
            "delivery_pending": artifacts.delivery_pending(decision_id),
        })

    def _handle_decision_resolve(self, decision_id: str):
        from lib import artifacts
        # This is a cooperative consent workflow, not a hostile-code sandbox:
        # local agents already share the host and SQLite file. `resolved_by`
        # records the direct user action by default. An on-behalf resolve is
        # enforced here: the acting session must be owner-configured in
        # [agents] decision_on_behalf_sessions and must present its own
        # verified turn identity; the artifacts layer also refuses the
        # asking agent itself.
        data = self._read_json()
        if not isinstance(data, dict): return self._json_error(400, "json object required")
        choice = data.get("choice")
        answer = data.get("answer")
        on_behalf_session = data.get("on_behalf_session")
        if on_behalf_session is not None and (
                not isinstance(on_behalf_session, str) or not on_behalf_session.strip()):
            return self._json_error(400, "on_behalf_session must be a nonempty session")
        if (choice is None) == (answer is None):
            return self._json_error(400, "exactly one of choice or answer is required")
        if choice is not None:
            if not isinstance(choice, str) or choice.strip().lower() not in {"accepted", "rejected"}:
                return self._json_error(400, "invalid decision choice")
            choice = choice.strip().lower()
        raw_revision = data.get("expected_revision")
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
            return self._json_error(400, "expected_revision must be an integer")
        revision = raw_revision
        # Turn identity is independent of the optional body field. A supplied
        # token must verify, and an identified agent can never resolve as user.
        caller_id = self._turn_caller()  # lookup failures remain retryable 503
        if self.headers.get("X-Clarp-Turn") is not None and not caller_id:
            return self._json_error(403, "verified turn identity required")
        if caller_id and on_behalf_session is None:
            return self._json_error(403, "agent resolution requires on_behalf_session")
        designated_sessions = ()
        verified_session = ""
        if on_behalf_session is not None:
            from lib import config, identity
            designated_sessions = config.load().decision_on_behalf_sessions
            caller = identity.lookup(caller_id) if caller_id else None
            verified_session = str(caller["session"]) if caller else ""
        try:
            row, changed = artifacts.resolve(
                decision_id, choice=choice, answer=answer, expected_revision=revision,
                on_behalf_session=on_behalf_session,
                designated_sessions=designated_sessions, verified_session=verified_session)
        except PermissionError as exc:
            return self._json_error(403, str(exc))
        except ValueError as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        self._deliver_pending_decisions()
        return self._json_ok({"artifact": row, "changed": changed,
                              "delivery_pending": artifacts.delivery_pending(decision_id)})

    def _handle_decision_withdraw(self, decision_id: str):
        """The asking agent revokes its own request; silent for both sides."""
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "json object required")
        session = data.get("session")
        if not isinstance(session, str) or not session.strip():
            return self._json_error(400, "session required")
        try:
            row, changed = artifacts.withdraw(decision_id, session=session)
        except PermissionError as exc:
            return self._json_error(403, str(exc))
        except ValueError as exc:
            return self._json_error(409, str(exc))
        self._broadcast_artifact(row)
        return self._json_ok({"artifact": row, "changed": changed})

    def _handle_pinned_artifacts(self):
        from lib import artifacts
        representation = self._query().get("representation", [""])[0]
        if representation not in artifacts.REPRESENTATIONS:
            return self._json_error(400, "unsupported artifact representation")
        with artifacts.summary_request() as budget:
            body = {"artifacts": [
                artifacts.response_representation(row, representation, budget=budget,
                                                  render_capable=row.get("type") == "render_spec" and _can_render(self))
                for row in artifacts.list_pinned()]}
        return self._json_ok(body)
