-- Pre-baked views for querying claude-pwa eventlogs.
-- Loaded by scripts/query.sh after attaching the JSONL+Parquet sources.
--
-- Schema (rolling row):
--   ts, source, event, level, session, backend_session_id, persona, clip_url,
--   duration_ms, detail (JSON struct)
--
-- See DEBUGGING.md for query recipes.

-- ---- base table ---------------------------------------------------

-- read_json_auto's schema only surfaces columns that appeared in the
-- sampled rows. Pin the full schema so a fresh log file (which may have
-- no herald or clip_url rows yet) doesn't blow up the views below.
-- ts is parsed as TIMESTAMPTZ so comparisons with `now()` work no matter
-- what the host timezone is.
CREATE OR REPLACE VIEW events AS
SELECT
    CAST(ts AS TIMESTAMPTZ)  AS ts,
    source,
    event,
    level,
    session,
    backend_session_id,
    persona,
    clip_url,
    duration_ms,
    trace_id,
    detail
FROM read_json(
    getenv('CLAUDE_PWA_LOG_DIR') || '/*.jsonl',
    format='newline_delimited',
    columns={
        ts: 'VARCHAR',
        source: 'VARCHAR',
        event: 'VARCHAR',
        level: 'VARCHAR',
        session: 'VARCHAR',
        backend_session_id: 'VARCHAR',
        persona: 'VARCHAR',
        clip_url: 'VARCHAR',
        duration_ms: 'DOUBLE',
        trace_id: 'VARCHAR',
        detail: 'JSON'
    },
    ignore_errors=true
);

-- ---- request log --------------------------------------------------

CREATE OR REPLACE VIEW requests AS
SELECT
    ts,
    detail->>'$.method'  AS method,
    detail->>'$.path'    AS path,
    CAST(detail->>'$.status' AS INTEGER) AS status,
    duration_ms,
    detail->>'$.client'  AS client
FROM events
WHERE source = 'server' AND event = 'httpRequest';

-- ---- herald lifecycle --------------------------------------------

CREATE OR REPLACE VIEW heralds AS
SELECT
    ts,
    event,                       -- heraldEmitted | heraldFlushed | heraldDeclined
    detail->>'$.msg' AS msg,     -- e.g. "rachel count=2"
    session,
    persona,
    detail
FROM events
WHERE event IN ('heraldEmitted', 'heraldFlushed', 'heraldDeclined');

-- ---- audio clip lifecycle ----------------------------------------
-- Each row is one mp3, with its generation / playback timestamps joined.

CREATE OR REPLACE VIEW audio_lifecycle AS
WITH synth AS (
    SELECT ts AS synthesized_at, clip_url AS clip, duration_ms AS synth_ms
    FROM events
    WHERE source = 'tts' AND event = 'synthOk'
),
broadcast AS (
    SELECT ts AS broadcast_at, clip_url AS clip
    FROM events
    WHERE source = 'audio_stream' AND event = 'broadcast'
),
queued AS (
    SELECT
        ts AS queued_at,
        coalesce(clip_url, detail->>'$.url') AS clip
    FROM events
WHERE source = 'client'
  AND event = 'clipAck'
  AND (detail->>'$.status') = 'queued'
),
play_started AS (
    SELECT
        ts AS play_started_at,
        coalesce(clip_url, detail->>'$.url') AS clip
    FROM events
WHERE source = 'client'
  AND event = 'clipAck'
  AND (detail->>'$.status') = 'play-start'
),
played AS (
    SELECT
        ts AS played_at,
        coalesce(clip_url, detail->>'$.url') AS clip
    FROM events
WHERE source = 'client'
  AND event = 'clipAck'
  AND (detail->>'$.status') = 'play-ok'
),
failed AS (
    SELECT
        ts AS failed_at,
        coalesce(clip_url, detail->>'$.url') AS clip,
        detail->>'$.error' AS error
    FROM events
WHERE source = 'client'
  AND event = 'clipAck'
  AND (detail->>'$.status') = 'play-fail'
)
SELECT
    coalesce(s.clip, b.clip, q.clip, ps.clip, p.clip, f.clip) AS clip_url,
    s.synthesized_at,
    b.broadcast_at,
    q.queued_at,
    ps.play_started_at,
    p.played_at,
    f.failed_at,
    f.error,
    s.synth_ms                                     AS synth_ms,
    datediff('milliseconds', s.synthesized_at, b.broadcast_at) AS synth_to_broadcast_ms,
    datediff('milliseconds', b.broadcast_at, q.queued_at)      AS broadcast_to_queue_ms,
    datediff('milliseconds', q.queued_at, ps.play_started_at)  AS queue_to_start_ms,
    datediff('milliseconds', ps.play_started_at, p.played_at)  AS start_to_played_ms
FROM synth s
FULL OUTER JOIN broadcast b USING (clip)
FULL OUTER JOIN queued q USING (clip)
FULL OUTER JOIN play_started ps USING (clip)
FULL OUTER JOIN played p USING (clip)
FULL OUTER JOIN failed f USING (clip);

-- ---- state machine transitions -----------------------------------

CREATE OR REPLACE VIEW state_transitions AS
SELECT
    ts,
    regexp_extract(detail->>'$.msg', '^state ([a-z]+)->([a-z]+)', 1) AS from_state,
    regexp_extract(detail->>'$.msg', '^state ([a-z]+)->([a-z]+)', 2) AS to_state,
    session,
    persona
FROM events
WHERE source = 'client' AND event LIKE 'state%';

-- ---- errors --------------------------------------------------------

CREATE OR REPLACE VIEW errors AS
SELECT
    ts,
    source,
    event,
    detail->>'$.error_type' AS error_type,
    detail->>'$.error'      AS error_message,
    detail->>'$.traceback'  AS traceback
FROM events
WHERE level = 'error';

-- ---- conversations: prompt → assistant text ----------------------
-- Joins the UserPromptSubmit fire (user spoke) to the next Stop-hook chunks
-- entry on the same app session.

CREATE OR REPLACE VIEW prompts AS
SELECT
    ts AS user_at,
    session,
    backend_session_id
FROM events
WHERE source = 'userprompt_hook' AND event = 'promptSubmit';

CREATE OR REPLACE VIEW stops AS
WITH stop_rows AS (
    SELECT ts, session, backend_session_id, detail->>'$.msg' AS msg
    FROM events
    WHERE source = 'stop_hook'
)
SELECT
    ts AS reply_at,
    session,
    backend_session_id,
    msg AS reply_msg
FROM stop_rows
WHERE msg LIKE 'chunks=%';

CREATE OR REPLACE VIEW conversations AS
SELECT
    p.user_at,
    s.reply_at,
    p.session,
    p.backend_session_id,
    s.reply_msg,
    datediff('milliseconds', p.user_at, s.reply_at) AS thinking_ms
FROM prompts p
ASOF LEFT JOIN stops s
    ON s.session = p.session
   AND s.reply_at > p.user_at;

-- ---- /transcribe round trips -------------------------------------

CREATE OR REPLACE VIEW transcripts AS
SELECT
    ts,
    duration_ms,
    detail->>'$.text' AS text,
    trace_id
FROM events
WHERE source = 'server' AND event = 'transcribe';

CREATE OR REPLACE VIEW trace_paths AS
SELECT
    trace_id,
    min(ts) AS first_at,
    max(ts) AS last_at,
    count(*) AS events,
    string_agg(source || ':' || event, ' -> ' ORDER BY ts) AS path
FROM events
WHERE trace_id IS NOT NULL
GROUP BY trace_id;
