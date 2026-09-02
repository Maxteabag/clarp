// Ordering for the desktop conversation rail.
//
// Extracted from the component for one reason: the rule is "the list moves
// when an agent does something, and never because you opened a chat", and
// that is not a claim you can check against a live roster of agents who are
// busy doing real work. Here it is a pure function over a snapshot.

/**
 * @param {Record<string, {name?: string}>} agentsBySession
 * @param {Record<string, {last_activity?: number}>} statusMap
 * @param {string[]} availableSessions
 * @returns {{sid: string, name: string, lastActivity: number}[]}
 */
export function orderChats(agentsBySession, statusMap, availableSessions) {
  const available = new Set(availableSessions || []);
  return Object.entries(agentsBySession || {})
    .filter(([sid, info]) => info && info.name && available.has(sid))
    .map(([sid, info]) => ({
      sid,
      name: info.name,
      lastActivity: Number((statusMap || {})[sid]?.last_activity) || 0,
    }))
    // Newest activity first. Note what is *not* in this comparator: the
    // focused session. Opening a chat changes app.session and nothing else,
    // so the order it produces is identical before and after.
    //
    // Agents that have never reported activity all share a timestamp of 0,
    // so they fall back to alphabetical rather than to whatever order the
    // object happened to enumerate in — otherwise the tail of the list could
    // reshuffle on any unrelated snapshot replacement.
    .sort((a, b) => (b.lastActivity - a.lastActivity) || a.name.localeCompare(b.name));
}
