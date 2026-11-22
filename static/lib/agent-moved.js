// An agent that moved to another Host (Host contract 125, feature agent_moved).
//
// A snapshot row carries `moved: {to: {host, id}, at}` or null. While moved, no turn of the
// agent starts on this Host: the chat stays readable, its row and header say where it went,
// the composer is held, and a send the Host refuses (409 agent_moved) is never sent again.
// Framework-free so the vitest suite can pin the wording every client shares.

const MAX_HOST = 64;

/** `{to: {host, id}, at}` or null, from a snapshot row's `moved`. */
export function normalizeMoved(value) {
  if (!value || typeof value !== 'object' || !value.to || typeof value.to !== 'object') return null;
  return {
    to: { host: String(value.to.host || ''), id: String(value.to.id || '') },
    at: Number(value.at) || 0,
  };
}

/** The destination's name when it is one a person can read, else ''. */
export function movedHost(moved) {
  const host = String(moved?.to?.host || '').trim();
  if (!host || host.length > MAX_HOST || /[\u0000-\u001f\u007f]/.test(host)) return '';
  return host;
}

/** 'Moved to <host>' (or 'Moved to another Host'), or '' for an agent that did not move. */
export function movedLine(moved) {
  if (!moved) return '';
  const host = movedHost(moved);
  return host ? `Moved to ${host}` : 'Moved to another Host';
}

/** What a send to a moved agent says, never sent again. */
export function movedSendText(name, moved) {
  const host = movedHost(moved);
  return `${name || 'This agent'} moved to ${host || 'another Host'}, so this was not sent.`;
}

/** The refusal's text when the Host answered a send `409 {error: "agent_moved"}`, else ''. */
export function movedRefusal(status, body, name) {
  if (status !== 409 || !body || body.error !== 'agent_moved') return '';
  return movedSendText(name, { to: body.moved_to || {} });
}
