// Core app state: which agent is focused, what every agent is doing, the
// connection indicator, and the transient status toast.
//
// This is the piece the old client did not have. Every `paint*()` helper in
// app.js existed because a state change had no way to reach the DOM on its
// own — miss a call site and the UI silently desynced. Here the state is
// `$state`, components read it, and Svelte does the reaching.

import { createAgentSnapshotStore } from '@core/agent-snapshot.js';
import { resolveAvatarUrl } from '@core/avatar.js';
import { chooseSession, visibleSessions } from '@core/session-select.js';
import { AgentState } from '@core/protocol.js';
import { clog } from '../lib/net.js';
import { ensureLoaded, reconcileWithSnapshot } from './conversations.svelte.js';

export const AVATAR_PALETTE = {
  Mike: '#7aa2f7', Rachel: '#f7768e', Domi: '#bb9af7', Bella: '#ff9e64',
  Antoni: '#7dcfff', Elli: '#c0a3e5', Josh: '#9ece6a', Arnold: '#9d7cd8',
  Adam: '#e0af68', Sam: '#73daca',
};

export const DEFAULT_ROSTER = [
  'Mike', 'Rachel', 'Domi', 'Bella', 'Antoni',
  'Elli', 'Josh', 'Arnold', 'Adam', 'Sam',
];

export const agentSnapshot = createAgentSnapshotStore();

export const app = $state({
  /** Empty until the snapshot names a live agent; never a guessed default. */
  session: localStorage.getItem('session') || '',
  availableSessions: [],
  agentsBySession: {},
  /** Mirrors agentSnapshot.asStatusMap(); replaced wholesale so reads track. */
  status: {},
  conn: 'connecting',
  showReconnect: false,
  toast: '',
  version: '',
  /** Bumped on every snapshot patch so time-dependent views re-derive. */
  tick: 0,
});

export const isDesktop = document.documentElement.classList.contains('desktop');

// ---- status toast -------------------------------------------------------

let toastTimer = null;
export function flash(msg, ms = 2500) {
  app.toast = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { app.toast = ''; }, ms);
}

// ---- connection ---------------------------------------------------------

let deadOverlayTimer = null;
export function setConn(state, deadOverlayMs) {
  app.conn = state;
  if (state === 'dead') {
    if (!deadOverlayTimer) {
      deadOverlayTimer = setTimeout(() => {
        app.showReconnect = true;
        deadOverlayTimer = null;
      }, deadOverlayMs);
    }
  } else {
    if (deadOverlayTimer) { clearTimeout(deadOverlayTimer); deadOverlayTimer = null; }
    app.showReconnect = false;
  }
}

// ---- derived reads ------------------------------------------------------

export function syncStatus() {
  const next = agentSnapshot.asStatusMap();
  app.status = { ...next };
  app.agentsBySession = { ...next };
  app.tick++;
}

export function chipLabel(sid) {
  return (app.agentsBySession[sid] || {}).name || sid;
}

export function avatarUrl(name, sid = '') {
  return resolveAvatarUrl(app.agentsBySession, name, sid);
}

export function statusFor(sid) {
  return app.status[sid] || {};
}

export function shortActivityText(s) {
  const a = s && s.activity;
  return (s && s.activity_summary) || (a && a.summary) || '';
}

export function shortActivityPhase(s) {
  const a = s && s.activity;
  return (s && s.activity_action) || (a && a.action)
      || (s && s.activity_phase) || (a && a.phase) || '';
}

// ---- unread badges ------------------------------------------------------

function readJSON(key) {
  try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (_) { return {}; }
}

export function isUserNotificationUnread(session) {
  const seen = readJSON('agentSeen');
  const notifications = readJSON('agentNotifications');
  return !!session && session !== app.session
    && (notifications[session] || 0) > (seen[session] || 0);
}

export function unreadAgentCount() {
  const seen = readJSON('agentSeen');
  const notifications = readJSON('agentNotifications');
  let count = 0;
  for (const sid of Object.keys(app.agentsBySession)) {
    if (sid === app.session) continue;
    if (!app.availableSessions.includes(sid)) continue;
    if ((notifications[sid] || 0) > (seen[sid] || 0)) count++;
  }
  return count;
}

export function rememberUserNotification(ev) {
  const sid = (ev && ev.session) || '';
  if (!sid || ev.badge === false || ev.unread === false) return;
  try {
    const notifications = readJSON('agentNotifications');
    const done = Number(ev.done_ts || ev.ts || 0);
    const ts = done > 10_000_000_000 ? Math.floor(done / 1000)
      : (done || Math.floor(Date.now() / 1000));
    notifications[sid] = Math.max(notifications[sid] || 0, ts);
    localStorage.setItem('agentNotifications', JSON.stringify(notifications));
    app.tick++;
  } catch (_) {}
}

// ---- agent banner -------------------------------------------------------

export function bannerFor(sid) {
  const s = statusFor(sid);
  const kind = s.latest_state || '';
  if (kind === AgentState.COMPACTING) {
    const trig = s.compacting_trigger === 'manual' ? ' (manual)' : '';
    return {
      cls: AgentState.COMPACTING,
      spinner: true,
      msg: shortActivityText(s) || `Compacting context${trig}`,
      startedAt: s.turn_started_at,
    };
  }
  if (kind === AgentState.WAITING) {
    return {
      cls: AgentState.WAITING,
      icon: '!',
      msg: shortActivityText(s) || s.waiting_message || 'Needs your attention',
    };
  }
  if (kind === AgentState.INTERRUPTED) {
    return {
      cls: AgentState.INTERRUPTED,
      icon: '!',
      msg: shortActivityText(s) || 'Turn interrupted — send again to resume',
    };
  }
  return null;
}

// ---- server calls -------------------------------------------------------

/**
 * The bootstrap call from docs/protocol.md. One snapshot refreshes the agent
 * list, the chat list, and tells the conversation store which cached chats
 * are behind or belong to a conversation that no longer exists.
 */
export async function refreshAgentSnapshot() {
  try {
    const r = await fetch('/agents/snapshot');
    if (!r.ok) return agentSnapshot.asStatusMap();
    const d = await r.json();
    const status = agentSnapshot.replaceFromSnapshot(d || {});
    syncStatus();
    const sessions = (d && d.agents || []).map(a => a.session).filter(Boolean);
    app.availableSessions = visibleSessions(sessions, status);
    reconcileWithSnapshot(d && d.agents || []);
    // A session that no longer exists (released, archived) cannot stay open.
    const next = chooseSession({
      sessions: app.availableSessions,
      serverDefault: app.availableSessions[0] || '',
      current: app.session,
    });
    if (next && next !== app.session) await setSession(next);
    return status;
  } catch (_) {
    return agentSnapshot.asStatusMap();
  }
}

/** Same call; the older name is still used by the overview and switcher. */
export function refreshSessions() {
  return refreshAgentSnapshot();
}

/** Open a chat: local state first, then tell the server where focus is. */
export async function setSession(name) {
  if (!name) return;
  const prev = app.session;
  app.session = name;
  try { localStorage.setItem('session', name); } catch (_) {}
  try {
    const seen = readJSON('agentSeen');
    seen[name] = Math.floor(Date.now() / 1000);
    localStorage.setItem('agentSeen', JSON.stringify(seen));
  } catch (_) {}
  app.tick++;
  ensureLoaded(name).catch(() => {});
  if (name !== prev) clog('sessionSwitch', `${prev} -> ${name}`);
  try {
    await fetch('/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: name }),
    });
  } catch (_) {}
}

/**
 * Server told us where focus is. Mirror it locally and never POST /select
 * back, or two clients feed each other's broadcasts forever.
 */
export function mirrorFocus(session, agentId) {
  agentSnapshot.setFocus(session || '', agentId || '');
  if (session && session !== app.session) {
    app.session = session;
    try { localStorage.setItem('session', session); } catch (_) {}
    ensureLoaded(session).catch(() => {});
  }
  syncStatus();
}

export function setVersion(serverVer, adapterVersion) {
  const s = serverVer ? String(serverVer).replace(/^claude-pwa-/, 'v') : 'v?';
  app.version = `${s} / ${adapterVersion}`;
}

export function logState(ev, machine) {
  clog('state', `${ev.from}->${ev.to}` +
    (machine.expectingFrom ? ` for=${machine.expectingFrom}` : ''));
}
