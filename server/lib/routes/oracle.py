"""OracleRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "oracle"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from lib.log import log, log_exception


class OracleRoutes:
    def _handle_oracle_call_close(self):
        from lib import oracle_calls
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        oracle_calls.close_call(str(self._request_principal), str(data.get("attempt_id") or ""))
        return self._json_ok({"closed": True})

    def _handle_oracle_call_create(self):
        from lib import oracle_calls, oracle_contact
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            result = oracle_calls.create_call(ctx=self.ctx,
                principal=str(self._request_principal),
                attempt_id=str(data.get("attempt_id") or ""), sdp=data.get("sdp"),
                fallback=oracle_contact.effective(data.get("oracle_session"), source="v1-webrtc"),
                stop=lambda session: self._stop_agent_session(session, strict=True, defer_finish=True)[1])
            return self._json_ok(result)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except Exception as exc:
            log_exception("oracleCallCreateFail", exc)
            return self._json_error(502, "Oracle connection unavailable")

    def _handle_oracle_call_status(self):
        from lib import oracle_calls
        query = self._query()
        rows = oracle_calls.call_results(str(self._request_principal), query.get("attempt_id", [""])[0])
        return self._json_ok({"delegations": rows})

    def _handle_oracle_connect(self):
        """Put the caller's Oracle call through to an agent, or back to Oracle.
        Body: {"agent": "<session|persona|oracle>", "principal"?}. See
        docs/oracle-handoff.md; waits until the phone settles the handoff."""
        from lib import oracle_handoffs
        principal = self._oracle_handoff_principal()
        if principal is None:
            return
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        oracle_handoffs.bind(self.ctx)
        try:
            result = oracle_handoffs.connect(principal, str(data.get("agent") or ""),
                                             principal=data.get("principal") or None)
        except oracle_handoffs.HandoffError as exc:
            return self._json(exc.status, exc.body())
        return self._json_ok(result)

    def _handle_oracle_contact_get(self):
        """The Host-owned Oracle contact, validated against the live roster."""
        from lib import oracle_contact
        return self._json_ok(oracle_contact.get())

    def _handle_oracle_contact_post(self):
        """Set or clear the Oracle contact. Body: {"session": "<session|persona|''>"}."""
        from lib import oracle_contact
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            result = oracle_contact.set(str(data.get("session") or ""))
        except ValueError as exc:
            return self._json_error(409, str(exc))
        log("oracleContactSet", f"session={result['session'] or '-'}")
        return self._json_ok(result)

    def _handle_oracle_context_image(self):
        from lib.oracle_live_calls import context_image
        return context_image(self)

    def _handle_oracle_delegation_ack(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            delegation_id = oracle_delegations.normalize_id(
                str(data.get("delegation_id") or ""))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        if not oracle_delegations.acknowledge(
                delegation_id,
                owner_principal=str(self._request_principal or "administrator")):
            return self._json_error(404, "terminal delegation not found")
        return self._json_ok({"ok": True})

    def _handle_oracle_delegation_cancel(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        principal = str(self._request_principal or "administrator")
        session = str(data.get("session") or "").strip()
        if session:
            try:
                rows = oracle_delegations.cancel_for_session(
                    session,
                    stop=lambda: self._stop_agent_session(
                        session, strict=True, defer_finish=True)[1],
                    owner_principal=principal)
            except ValueError as exc:
                return self._json_error(400, str(exc))
            except Exception as exc:  # noqa: BLE001
                log_exception("oracleAgentCancelFail", exc, detail=session)
                return self._json_error(502, "agent cancellation failed")
            return self._json_ok({
   "cancelled_count": len(rows), "delegations": rows,
            })
        try:
            delegation_id = oracle_delegations.normalize_id(
                str(data.get("delegation_id") or ""))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        try:
            row = oracle_delegations.cancel(
                delegation_id, owner_principal=principal)
        except oracle_delegations.DelegationNotCancellable as exc:
            return self._json_error(409, str(exc))
        if row is None:
            return self._json_error(404, "delegation not found")
        return self._json_ok({"delegation": row})

    def _handle_oracle_delegation_create(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            row = oracle_delegations.dispatch(
                ctx=self.ctx,
                delegation_id=str(data.get("delegation_id") or ""),
                session=str(data.get("session") or ""),
                request_text=str(data.get("request") or ""),
                authenticated_at_admission=bool(
                    getattr(self, "_request_auth_validated", False)
                    and (getattr(self.ctx, "auth_token", "") or "")
                ),
                owner_principal=str(self._request_principal or "administrator"),
            )
        except oracle_delegations.DelegationCollision as exc:
            return self._json_error(409, str(exc))
        except (ValueError, LookupError) as exc:
            return self._json_error(400 if isinstance(exc, ValueError) else 404, str(exc))
        except Exception as exc:  # noqa: BLE001
            status = int(getattr(exc, "status", 500))
            log_exception("oracleDelegationFail", exc)
            return self._json_error(status, str(exc))
        return self._json_ok({"delegation": row})

    def _handle_oracle_delegations_get(self):
        from lib import oracle_delegations, turn_dispatch
        oracle_delegations.reconcile_orphans(
            is_live=turn_dispatch.owns_inflight_trace)
        query = self._query()
        try:
            limit = int(query.get("limit", ["50"])[0] or "50")
        except ValueError:
            limit = 50
        include_all = str(query.get("all", ["0"])[0]).lower() in {
            "1", "true", "yes",
        }
        principal = str(self._request_principal or "administrator")
        rows = (oracle_delegations.recent(
                    owner_principal=principal, limit=limit) if include_all
                else oracle_delegations.undelivered(
                    owner_principal=principal, limit=limit))
        return self._json_ok({"delegations": rows})

    def _handle_oracle_handoff_ack(self):
        from lib import oracle_handoffs
        principal = self._oracle_handoff_principal()
        if principal is None:
            return
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        oracle_handoffs.bind(self.ctx)
        try:
            return self._json_ok(oracle_handoffs.ack(principal, data))
        except oracle_handoffs.HandoffError as exc:
            return self._json(exc.status, exc.body())

    def _handle_oracle_handoff_get(self):
        from lib import oracle_handoffs
        principal = self._oracle_handoff_principal()
        if principal is None:
            return
        if principal == oracle_handoffs.ADMINISTRATOR:
            principal = (self._query().get("principal") or [principal])[0]
        return self._json_ok(oracle_handoffs.snapshot(principal))

    def _handle_oracle_live_call_close(self):
        from lib.oracle_live_calls import handle
        return handle(self, "close")

    def _handle_oracle_live_call_control(self):
        from lib.oracle_live_calls import handle
        return handle(self, "control")

    def _handle_oracle_live_call_create(self):
        from lib.oracle_live_calls import handle
        return handle(self, "create")

    def _handle_oracle_live_call_status(self):
        from lib.oracle_live_calls import handle
        return handle(self, "status")

    def _handle_oracle_status(self):
        from lib.oracle_realtime import capability
        return self._json_ok(capability())

    def _handle_oracle_voice_provider_get(self):
        """Which voice model Oracle v2 calls use (docs/oracle-gemini-live.md)."""
        from lib import config, oracle_voice_provider
        return self._json_ok(oracle_voice_provider.status(config.load()))

    def _handle_oracle_voice_provider_post(self):
        """Choose the Oracle v2 voice model. Body: {"provider": "openai"|"gemini"}.
        Applies to the next call; a call in progress keeps its model."""
        from lib import config, oracle_voice_provider
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            oracle_voice_provider.set_provider(data.get("provider"))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        result = oracle_voice_provider.status(config.load())
        log("oracleVoiceProvider", f"selected={result['selected']} effective={result['provider']}")
        return self._json_ok(result)

    def _oracle_auth_missing(self, path: str) -> bool:
        return (path == "/oracle" or path.startswith("/oracle/")) and not bool(
            getattr(self, "_request_auth_validated", False))

    def _oracle_handoff_principal(self):
        """The full-scope caller, or None after answering 401."""
        from lib.http_utils import principal_of, require_full_scope
        who = principal_of(self)
        denied = require_full_scope(who, message="Oracle handoffs require full-device authentication")
        if denied:
            self._json_error(401, denied)
            return None
        return who.principal
