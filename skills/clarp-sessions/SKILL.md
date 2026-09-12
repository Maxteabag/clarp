---
name: clarp-sessions
description: Resolve Clarp agents and sessions, or resume an existing native conversation as a named Clarp agent on the same Host. Use for requests like "make this session Theo", before targeting another agent, or when diagnosing session state.
---

# Clarp Sessions

Read the canonical local database through the platform-aware admin command:

```bash
clarp-admin sessions
```

Use the `session` slug for Clarp API requests. Do not guess a session from a
persona when more than one row could match.

## Resume this conversation as a Clarp agent

For "start Theo with this conversation", keep the native conversation ID and
backend. The supported path is `POST /agents` with `resume_session_id`, followed
by `GET /log` to verify the history. This needs no translation, summary prompt,
terminal window, deployment, or server restart.

1. **Identify the source once, in the original conversation.** For Codex, use
   `CODEX_THREAD_ID` (or `CODEX_SESSION_ID`), then verify that the native
   transcript's session metadata has that ID and the expected working directory.
   Check a recognizable recent user request and assistant answer. Resolve this
   before delegating: a helper agent's environment identifies its own conversation.
   For Claude, use its known native session ID and verify the matching transcript;
   if no reliable ID is available, locate the transcript by its content. A title,
   the newest file, or a Clarp slug alone does not identify the source. Preserve
   the user's requested directory, backend, and explicit model/effort choices.

2. **Resolve the Host and existing target.** `clarp-admin paths` returns the
   installed configuration and database paths. Read the TOML configuration's
   `[server].bind_addr`, `port`, and `auth_token`. Use the local bind address for
   API requests; wildcard `0.0.0.0`/`::` bindings need a connectable loopback
   address, with IPv6 literals bracketed in URLs. Keep the token inside the HTTP
   client's `Authorization: Bearer ...` header; do not print it or a token-bearing
   URL. Check `clarp-admin sessions` for the requested persona. If it already has
   a session, inspect that session's `/log`: reuse it when its `conversation_id`
   matches the source. A different conversation is an occupied target, not
   permission to replace it. Do not overwrite, archive, or delete it implicitly.

3. **Create once using the verified native ID.** For example:

   ```http
   POST /agents
   Authorization: Bearer <token read privately from configuration>
   Content-Type: application/json

   {
     "name": "Theo",
     "backend": "codex",
     "cwd": "/absolute/path/to/project",
     "resume_session_id": "<verified native conversation UUID>",
     "synthesize_audio": false
   }
   ```

   Substitute the user's persona and verified source values. Omit `session`,
   `replace_sid`, and `fork_session_id`: let Clarp mint a fresh slug and inherit
   the existing persona's voice and appearance. The response is
   `{"ok": true, "session": "theo-…", "name": "Theo"}`. Keep its actual slug.
   `synthesize_audio: false` suppresses the spoken creation announcement. Creation
   opens a ready runtime and binds the native ID; it does not dispatch a model
   turn. A user asking to start or move this session authorizes this creation.

4. **Verify through the same conversation API the app uses.** Request:

   ```http
   GET /log?session=<returned-slug>&limit=100&include_tool_details=0
   ```

   Authenticate as above and URL-encode the slug. Require `conversation_id` to
   equal the source UUID, `missing` to be false, and `turns` to contain recognizable
   recent source content. This endpoint imports the native transcript into
   Clarp's conversation read model; no test prompt is needed. Check the final
   roster row's persona, backend, and directory with `clarp-admin sessions`.
   If history is missing or mismatched, report that the handoff is not verified
   and investigate transcript visibility before dispatching work. Do not claim
   success from the creation response alone.

5. **Finish briefly.** Tell the user which agent to open and that the exact
   conversation was verified. Let the current turn finish before continuing
   work through the target; avoid concurrent writers to the same native session.

If creation returns `contact_occupied`, `session_in_use`, or `voice_in_use`, inspect
the existing owner and preserve it. If a POST times out, reconcile the roster and
its conversation IDs before retrying; the first request may already have created
the agent. Never bypass conflicts by writing the database directly.

This fast path requires the existing transcript to be accessible to the selected
backend on that Host. Moving between Hosts or backend types needs a separate
transfer/conversion workflow; binding an unavailable UUID does not transfer
history. For ordinary same-Host resume, the API and checks above are sufficient:
inspect server source only if the deployed API disagrees.
