// 'Show chats from all Hosts' (issue 651), the web app's copy of the rules every client shares
// (clarp-ios ClarpNative/Models/AllHostsReading.swift, the Rust desktop's roster merge):
//
// - a device preference, never a Host's, with a one-time default and its guard;
// - which Hosts are read (the primary, the Host serving this app, first; then saved order);
// - the badge rule (the setting, and more than one saved Host) and its text on a moved row;
// - a moved agent's old row is left out when its destination is read, matched by server_id;
// - the quiet '<Host> unreachable' line, only while more than one Host is read;
// - the merged order: last activity across Hosts, then the primary first, then saved order.
//
// Framework-free so the vitest suite can pin it.

export const PREFERENCE_KEY = 'chats.allHosts.v1';
export const MIGRATION_KEY = 'chats.allHosts.migrated.v1';
export const PRIMARY = 'primary';

/**
 * What the first launch of this build writes, or null: with more than one saved Host the option
 * is switched on once; with one it stays off. Never again afterwards, and not while the saved
 * Hosts cannot be read (an unreadable store would count none and lose the default).
 */
export function migration({ stored = null, migrated = false, savedHosts = 0, storeReadable = true } = {}) {
  if (migrated || !storeReadable) return null;
  return { on: savedHosts > 1 || stored === true, migrated: true };
}

/** Reads the preference from a localStorage-like store, running the one-time default first. */
export function resolveAllHosts(store, savedHosts, storeReadable = true) {
  const raw = safeGet(store, PREFERENCE_KEY);
  const stored = raw === '1' ? true : raw === '0' ? false : null;
  const next = migration({ stored, migrated: safeGet(store, MIGRATION_KEY) === '1', savedHosts, storeReadable });
  if (next) {
    safeSet(store, PREFERENCE_KEY, next.on ? '1' : '0');
    safeSet(store, MIGRATION_KEY, '1');
    return next.on;
  }
  return stored ?? false;
}

function safeGet(store, key) { try { return store.getItem(key); } catch (_) { return null; } }
function safeSet(store, key, value) { try { store.setItem(key, value); } catch (_) {} }

/**
 * The Hosts read, as ids: the primary first, then the saved Hosts in saved order. Off: the
 * primary alone. `saved` is the saved other Hosts' ids, in saved order.
 */
export function readIds(saved, showAll) {
  return showAll ? [PRIMARY, ...saved.filter(id => id && id !== PRIMARY)] : [PRIMARY];
}

/** The badge text for a Host name, or '' (not shown): the setting on and more than one saved Host. */
export function hostLabel(name, showHost, savedHosts) {
  const trimmed = String(name || '').trim();
  return showHost !== false && savedHosts > 1 && trimmed ? trimmed : '';
}

/**
 * A moved agent's old row is left out when its destination is read: the destination's own row
 * stands for it. Matched by server_id, never by name; a move without a destination id stays.
 * `moved` is agent-moved.js's normalized `{to: {host, id}}`; `listed` the read Hosts' server_ids.
 */
export function superseded(moved, listed) {
  const id = String(moved?.to?.id || '');
  return !!id && listed.has(id);
}

/** What the badge names on a row: its Host, or a moved row's destination when that is not read. */
export function badgeHost(rowHost, moved, listed) {
  if (!moved || superseded(moved, listed)) return rowHost;
  const destination = String(moved.to?.host || '').trim();
  return destination || rowHost;
}

/** The read Hosts that get the quiet line: offline or refused, and only while more than one is read. */
export function unreachable(ids, states) {
  if (ids.length <= 1) return [];
  return ids.filter(id => states[id] === 'offline' || states[id] === 'refused');
}

export const unreachableLine = name => `${name} unreachable`;

/**
 * One list out of every read Host's rows. `groups`: [{hostId, rows: [{sid, name, lastActivity,
 * moved}]}] in read order (the primary first). Rows whose move is superseded are left out.
 * Order: last activity, newest first; then the primary first, then saved order; then the name.
 */
export function mergeChats(groups, listed) {
  const out = [];
  groups.forEach((group, order) => {
    for (const row of group.rows || []) {
      if (superseded(row.moved, listed)) continue;
      out.push({ ...row, hostId: group.hostId, hostOrder: order, key: `${group.hostId}:${row.sid}` });
    }
  });
  return out.sort((a, b) => ((Number(b.lastActivity) || 0) - (Number(a.lastActivity) || 0))
    || a.hostOrder - b.hostOrder
    || String(a.name).localeCompare(String(b.name)));
}

/** A saved Host's address as an origin (scheme://host[:port]), or '' when it is not one. */
export function originOf(url) {
  let text = String(url || '').trim();
  if (!text) return '';
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = 'https://' + text;
  try {
    const parsed = new URL(text);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    if (parsed.username || parsed.password) return '';
    return parsed.origin;
  } catch (_) {
    return '';
  }
}
