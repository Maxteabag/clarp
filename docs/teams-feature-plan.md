# Agent Teams Feature Plan

## Requirements Summary

Add first-class agent teams to Clarp so the user can group agents, view a team feed, and let important spoken summaries propagate between teammates.

The core product rule is: team sharing is based on the assistant's `<speak>...</speak>` content, but it must not depend on whether the phone is muted or whether TTS audio was synthesized. Mute is only a playback/client preference.

Initial scope:

- Teams are stored server-side in SQLite as the canonical source of truth.
- Agents can belong to zero or more teams.
- The native app has a Teams page, team detail view, and a way to assign an agent from the agent row/profile.
- Each team has a readable team chat/feed made from important updates.
- Agents become aware of teammate updates through a bounded digest injected into their next turn.
- The implementation avoids proactive cross-agent loops in the first version.

Out of scope for the first implementation:

- Drag-and-drop member assignment polish.
- Proactive "wake teammate now" dispatch.
- External Microsoft Teams integration.
- Global enterprise permissions or multi-user auth.

## Current Repo Anchors

- Top-level native navigation is `TabView` in `ios-native/ClarpNative/Views/ContentView.swift`; current tabs are Chats, Updates, Voice, Settings.
- `AppTab` currently has `chats`, `updates`, `voice`, and `settings` in `ios-native/ClarpNative/Models/ClarpModels.swift`.
- Agent row swipe actions already exist in `ios-native/ClarpNative/Views/AgentSwitcherView.swift`, so "Team" assignment can fit beside Configure/Delete.
- Agent profile is in `ios-native/ClarpNative/Views/ContentView.swift` as `AgentProfileView`; it already hosts media/profile sections.
- Native networking is centralized in `ios-native/ClarpNative/Networking/APIClient.swift` and the app-facing protocol is `ios-native/ClarpNative/Services/AppServiceProtocols.swift`.
- SQLite migrations live in `server/lib/db.py`; current schema version is 23 and recent migrations already use SQLite as the authoritative index for media.
- The durable chat read model is `server/lib/message_store.py`; assistant rows are inserted/updated by `store_transcript_turns`.
- Server `/send` accepts `synthesize_audio` and `hands_free` in `server/server.py`; muted native sends already pass `synthesizeAudio: !muted`.
- Codex TTS extraction currently gates on `latest_turn_synthesize_audio` in `server/lib/codex_runner.py`, so Teams must use a separate extractor path that does not depend on audio synthesis.

## Decision Drivers

1. Keep SQLite canonical so team state and delivery state are inspectable and recoverable.
2. Decouple "important update" capture from audio generation so muted mode still feeds teams.
3. Avoid autonomous agent chatter loops; start with next-turn digest injection.
4. Keep the native UX WhatsApp-like: Teams page for groups, swipe/profile for assignment, team detail as a chat-style feed.
5. Ship in slices that are testable without needing live agents to talk to each other.

## Options Considered

### Option A: Team feed from `<speak>` blocks in messages, digested into teammates' next turn

Store teams and team messages in SQLite. When assistant messages arrive, extract `<speak>` blocks from the raw assistant text into `team_messages`. For each teammate, create inbox rows. Before a new turn starts, inject a compact digest of unread team inbox items into that agent's context.

Pros:

- Works even when muted because it observes message text, not TTS queue rows.
- Durable and debuggable.
- Low loop risk because teammates do not immediately respond unless the user talks to them.
- Easy to test with message-store unit tests and simulated native tests.

Cons:

- Teammates only learn on their next turn, not immediately.
- Requires adding backend context injection for Claude and Codex paths.

### Option B: Use `tts_queue` as the source of team updates

Fan out team messages from queued spoken audio rows.

Pros:

- Reuses existing `<speak>` extraction and dedupe.
- Lower initial backend work.

Cons:

- Wrong product semantics: muted turns skip queueing.
- Failed TTS would silently drop team knowledge.
- Couples team intelligence to audio playback infrastructure.

Rejected for first implementation.

### Option C: Proactively send team messages to every teammate as silent turns

When one agent speaks an update, automatically dispatch that update to all teammates.

Pros:

- Teammates learn immediately.
- Feels like a live group chat.

Cons:

- High risk of loops and noisy agent work.
- Harder to explain and test.
- Can create surprising spend/tool usage.

Rejected for first implementation; keep as a later opt-in action.

## ADR

Decision: Implement Option A first.

Drivers: muted-mode correctness, SQLite observability, low loop risk, and a WhatsApp-like UX.

Alternatives considered: `tts_queue` fanout and proactive teammate turns.

Why chosen: the `<speak>` text is the semantic "important update" surface, while TTS/audio is only one delivery channel. Persisting team messages and inbox delivery state separately lets the app show teams, lets diagnostics explain what happened, and lets agents receive context without triggering surprise background work.

Consequences:

- We need one new server module for team persistence and one separate voice-markup extractor for team messages.
- Agent prompts/turn setup need a small team digest hook.
- Native models and API protocol gain team types/endpoints.
- Teams can be shipped without solving live proactive collaboration yet.

Follow-ups:

- Drag-and-drop assignment.
- Urgent team pings that proactively wake teammates.
- Team summaries and attention scoring on the Updates page.
- Team-specific mute/importance controls.

## Data Model

Add migration v24 in `server/lib/db.py`:

```sql
CREATE TABLE teams (
  team_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  archived_at INTEGER
);

CREATE TABLE team_members (
  team_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL,
  PRIMARY KEY (team_id, agent_id)
);

CREATE TABLE team_messages (
  team_message_id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  source_agent_id TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  trace_id TEXT,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(team_id, source_message_id, text)
);

CREATE TABLE team_inbox (
  team_message_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unread',
  injected_at INTEGER,
  read_at INTEGER,
  PRIMARY KEY (team_message_id, agent_id)
);
```

Rules:

- Do not create inbox rows for the source agent.
- Dedupe by `(team_id, source_message_id, text)` so streaming/final transcript copies do not duplicate.
- Keep deleted/archived teams soft-deleted for traceability.
- Keep team-message text stripped of voice-only fillers but preserve the actual spoken gist.

## Server API

Add endpoints in `server/server.py`:

- `GET /teams` -> teams with member agent IDs and recent preview.
- `POST /teams` -> create team.
- `PATCH /teams/<team_id>` -> rename/archive.
- `GET /teams/<team_id>/messages?limit=100&after=<id-or-ts>` -> feed.
- `PUT /teams/<team_id>/members/<agent_id>` -> add member.
- `DELETE /teams/<team_id>/members/<agent_id>` -> remove member.
- `GET /agents/<session>/teams` or include team memberships in `/agents/snapshot`.

Prefer including memberships in `/agents/snapshot` once the server model is stable, so the native app does not waterfall network calls on every list render.

## Team Message Capture

Add `server/lib/team_store.py`:

- CRUD teams and memberships.
- `extract_speak_blocks(raw_text: str) -> list[str]`, sharing grammar with `server/lib/voice_markup.py` but not the TTS queue.
- `capture_assistant_message(agent_id, message_id, trace_id, raw_text)`:
  - find teams for `agent_id`;
  - extract speak blocks;
  - insert `team_messages`;
  - create `team_inbox` rows for every teammate except source.

Call capture from `server/lib/message_store.py` after assistant messages are inserted or updated in `store_transcript_turns`. Also call it from live-message handling if we want team feeds to update before transcript finalization; if that adds duplication risk, start with durable transcript rows only.

## Agent Awareness

Add a bounded digest function in `server/lib/team_store.py`:

- `pending_team_digest(agent_id, limit=5) -> str`
- `mark_digest_injected(agent_id, message_ids)`

Inject before spawning a backend turn:

- In `server/lib/turn_dispatch.py`, build a team-context preamble for the target `agent_id`.
- Append it to the user prompt as a system-style context block or pass through backend-specific preamble plumbing.
- Include source agent name, team name, timestamp, and concise spoken text.
- Mark inbox rows as injected only after spawn is accepted.

Digest shape:

```text
Team updates since your last turn:
- [Design] Lena: Fixed the image cache path; gallery open is instant now.
- [Ops] Arnold: Deployment is running and needs no action.

Use this only as background context. Do not respond to teammates unless the user asks or it directly affects this request.
```

Loop controls:

- Team digest text must not itself be captured as a new team message.
- Do not fan out messages generated solely from team digest context unless the assistant explicitly produces a new `<speak>` block in response to the user.
- Do not inject a source agent's own message back to them.

## Native UX

Add `TeamsView` and `TeamDetailView`:

- New tab: `Teams`, likely between Updates and Voice.
- Team list cards show name, member avatars, newest team update, unread count.
- Team detail is a chat-style feed of `team_messages`.
- Create team sheet: name plus color.
- Member editor initially uses tap-to-add/remove avatars; drag/drop can come later.

Agent assignment:

- Add a "Team" swipe action in `AgentSwitcherView` trailing actions.
- Add a team membership section in `AgentProfileView`.
- Reuse the existing sheet style from agent configuration rather than inventing a separate modal stack.

Selected-tab migration:

- Add `AppTab.teams`.
- Bump selected-tab schema in `AppModel.initialSelectedTab()` so existing users keep Voice/Settings mapping.

## Implementation Steps

1. Backend schema and store:
   - Add v24 migration in `server/lib/db.py`.
   - Add `server/lib/team_store.py`.
   - Add unit tests for create/rename/archive, membership changes, dedupe, inbox fanout, and source-agent exclusion.

2. Capture important updates:
   - Add speak-block extraction independent of `tts_queue`.
   - Call team capture after assistant message persistence in `server/lib/message_store.py`.
   - Add regression test: muted turn with `<speak>` still creates `team_messages`; turn without `<speak>` does not.

3. Server API:
   - Add `/teams` routes in `server/server.py`.
   - Add JSON model tests for list/create/update/members/messages.
   - Include memberships in snapshot or add a compact memberships endpoint.

4. Agent awareness:
   - Add pending team digest lookup and injected-state marking.
   - Inject digest into `TurnDispatchService` before `spawn_turn`.
   - Test no self-delivery, bounded digest size, and marking only after accepted dispatch.

5. Native models and networking:
   - Add `Team`, `TeamMember`, `TeamMessage`, and response types.
   - Extend `AppAPIClient`, `APIClient`, and simulated client.
   - Add `AppModel` team state, loading, create/update/member functions.

6. Native Teams page:
   - Add `AppTab.teams` and selected-tab migration.
   - Add `TeamsView` and `TeamDetailView`.
   - Add create team sheet and initial member editor.

7. Assignment surfaces:
   - Add "Team" swipe action in `AgentSwitcherView`.
   - Add team membership section in `AgentProfileView`.
   - Keep the first version tap-based; drag/drop can be a follow-up.

8. Verification and polish:
   - Add simulated app tests for create team, add member, list feed, profile assignment.
   - Run Python unit tests for team store/API.
   - Run native typecheck/core tests/build.
   - Manually verify muted mode: agent `<speak>` creates a team feed item without playing audio.

## Acceptance Criteria

- A user can create, rename, and archive a team.
- A user can add/remove agents from a team from both the Teams page and an agent surface.
- Team page shows latest team updates in reverse chronological order.
- Team messages are created from assistant `<speak>` blocks even when native `muted == true`.
- Team messages are not created from ordinary written text outside `<speak>`.
- Source agents do not receive their own team message in their inbox.
- Duplicate live/final assistant copies do not create duplicate team messages.
- Teammates receive a bounded team digest in their next turn.
- Team digest injection does not itself create a new team-message loop.
- Existing Chats, Updates, Voice, Settings tabs keep their intended selected-tab migration.
- Tests cover server storage/capture/API and native model/UI state.

## Risks and Mitigations

- Risk: duplicate team messages from live and durable transcript paths.
  Mitigation: use `UNIQUE(team_id, source_message_id, text)` and initially capture only durable rows if needed.

- Risk: muted turns fail to propagate because code reuses TTS gating.
  Mitigation: never read from `tts_queue`; parse assistant message text directly.

- Risk: agent feedback loops.
  Mitigation: digest on next user turn only, no proactive dispatch in v1, and source-agent exclusion.

- Risk: teams tab migration moves existing users to the wrong tab.
  Mitigation: bump selected-tab schema and test stored raw values.

- Risk: broad branch conflicts with active rendering work.
  Mitigation: keep the current branch plan-only until the rendering work lands, then implement in a fresh branch or rebase this worktree.

## Verification Plan

Server:

- `pytest tests/unit/test_db_migrations.py`
- New `tests/unit/test_team_store.py`
- New API route tests for `/teams`
- Regression test proving muted `<speak>` capture works independently of `tts_queue`

iOS/native:

- `./ios-native/scripts/typecheck_core.sh`
- `./ios-native/scripts/test_core.sh`
- `./ios-native/scripts/build_ios.sh`
- Simulated app tests for team list, assignment, and feed loading.

Manual:

- Create team "Core".
- Add Lena and Omar.
- Send a muted Lena reply with `<speak>Important update</speak>`.
- Verify "Core" feed shows the update.
- Send a message to Omar.
- Verify Omar receives a concise team digest as context and no automatic proactive reply is spawned.
