export const SSEType = Object.freeze({
  AUDIO: 'audio',
  SERVER_VERSION: 'server-version',
  REMOTE_ACTION: 'remote-action',
  AGENT_STATE: 'agent-state',
  AGENT_ACTIVITY: 'agent-activity',
  AGENT_ROSTER: 'agent-roster',
  AGENT_FOCUS: 'agent-focus',
  TRANSCRIPT_UPDATED: 'transcript-updated',
  USER_NOTIFICATION: 'user-notification',
  QUEUE_UPDATED: 'queue-updated',
  ARTIFACT_UPDATED: 'artifact-updated',
  ATTENTION_UPDATED: 'attention-updated',
  BACKGROUND_JOB_UPDATED: 'background-job-updated',
  PROVIDER_LIMIT: 'provider-limit',
  // Voice synthesis failed (e.g. ElevenLabs quota exceeded) — shown to the user.
  TTS_ERROR: 'tts-error',
  // Native clients surface an explicit one-tap location sharing prompt.
  LOCATION_REQUEST: 'location-request',
  // Native clients surface an EventKit-backed calendar creation request.
  CALENDAR_REQUEST: 'calendar-request',
});

export const AgentBackend = Object.freeze({
  CLAUDE: 'claude',
  CODEX: 'codex',
  AGY: 'agy',
  GROK: 'grok',
  OPENCODE: 'opencode',
  // Branded ids this client ships marks for. Not the wire allow-list —
  // `/agent-model-options` is. Extra Host providers still render.
  VALID: new Set(['claude', 'codex', 'agy', 'grok', 'opencode']),
});

export const AgentState = Object.freeze({
  THINKING: 'thinking',
  TOOL: 'tool',
  IDLE: 'idle',
  SPAWNED: 'spawned',
  STOPPED: 'stopped',
  // New hook-driven kinds:
  // - DONE: Stop hook fired → turn complete, drives the unread badge
  // - COMPACTING: PreCompact hook → context compaction in progress (busy)
  // - WAITING: Notification hook → agent paused for user input / permission
  // - INTERRUPTED: a turn was cut short and not recovered (connection dropped
  //   and retries exhausted, API overloaded/rate-limited, or deliberate abort)
  DONE: 'done',
  COMPACTING: 'compacting',
  WAITING: 'waiting',
  INTERRUPTED: 'interrupted',
  // - BACKGROUND: an out-of-band task is running (e.g. watching a CI build).
  //   NOT busy — distinct from idle/done so the UI shows a neutral indicator.
  BACKGROUND: 'background',
  BUSY: new Set(['thinking', 'tool', 'compacting']),
  TURN_END: new Set(['idle', 'done']),
});

export const ActivityStatus = Object.freeze({
  RUNNING: 'running',
  OK: 'ok',
  ERROR: 'error',
  RECORDED: 'recorded',
  VALID: new Set(['running', 'ok', 'error', 'recorded']),
});

// Server-side enum — mirrored here so the contract test stays strict.
// The client doesn't usually read TurnSource directly; it appears inside
// agent-state event details when the server tags a turn pwa-vs-local.
export const TurnSource = Object.freeze({
  PWA: 'pwa',
  LOCAL: 'local',
  PWA_VOICE_MARKER: 'pwa-voice',
});

export const ClientAction = Object.freeze({
  RECORD: 'record',
  RECORD_TOGGLE: 'record-toggle',
  STOP_AGENT: 'stop-agent',
  VALID: new Set(['record', 'record-toggle', 'stop-agent']),
});

export const ClipStatus = Object.freeze({
  SYNTHESIZED: 'synthesized',
  BROADCAST: 'broadcast',
  QUEUED: 'queued',
  PLAY_START: 'play-start',
  PLAY_OK: 'play-ok',
  PLAY_FAIL: 'play-fail',
  HELD: 'held',
  VALID: new Set(['synthesized', 'broadcast', 'queued', 'play-start', 'play-ok', 'play-fail', 'held']),
});

export const ClipProducerStatus = Object.freeze({
  STREAMING: 'streaming',
  COMPLETE: 'complete',
  FAILED: 'failed',
  VALID: new Set(['streaming', 'complete', 'failed']),
});

export const Timing = Object.freeze({
  SERVICE_WORKER_UPDATE_MS: 5 * 60 * 1000,
  CLIENT_LOG_FLUSH_MS: 500,
  DEAD_OVERLAY_MS: 15000,
  AUDIO_CONTEXT_CLOSE_MS: 300,
  AWAIT_DEADLINE_MS: 60000,
  SSE_RECONNECT_BASE_MS: 250,
  SSE_RECONNECT_MAX_MS: 5000,
  SSE_STALE_MS: 25000,
  SSE_STALE_CHECK_MS: 5000,
  CHATBAR_UPDATE_MS: 100,
  CHAT_CLOSE_DOCK_LIFT_MAX_MS: 1400,
  MEDIA_RECORDER_TIMESLICE_MS: 250,
  VAD_ENERGY_ON: 50,
  VAD_ENERGY_OFF: 22,
  VAD_ENERGY_ON_MS: 250,
  SILENCE_END_MS: 1800,
  MIN_UTTER_MS: 500,
  GRACE_MS: 4000,
  CAPTURE_STOP_WATCHDOG_MS: 3000,
  TRANSCRIBING_FLASH_MS: 800,
  TRANSCRIBE_TIMEOUT_MS: 45_000,
});
