// Session bootstrap choice.
//
// `/sessions` answers with Agent sessions (including archived rows) plus a
// `default` carried over from config. That default is not guaranteed to name a
// visible Agent — once
// the configured session has no agent row, selecting it makes `/select` answer
// 404 and the client settles on a session the server has never heard of. Pick
// only from sessions the server actually listed.

/**
 * Decide which session to select on load or roster refresh.
 *
 * @param {{sessions?: unknown, serverDefault?: unknown, current?: unknown}} input
 * @returns {string|null} session to select, or null to keep the current one
 */
export function chooseSession({ sessions, serverDefault, current } = {}) {
  const live = Array.isArray(sessions)
    ? sessions.filter(s => typeof s === 'string' && s)
    : [];
  // Nothing real to pick from — keep whatever is showing rather than
  // selecting a session the server would reject.
  if (!live.length) return null;
  if (live.includes(current)) return null;
  if (typeof serverDefault === 'string' && live.includes(serverDefault)) {
    return serverDefault;
  }
  return live[0];
}

/** Keep archived Chats out of daily navigation while retaining them in the
 * full snapshot for the Archive surface. */
export function visibleSessions(sessions, agentsBySession = {}) {
  return (Array.isArray(sessions) ? sessions : []).filter(session => {
    if (typeof session !== 'string' || !session) return false;
    return !agentsBySession[session]?.archived_at;
  });
}
