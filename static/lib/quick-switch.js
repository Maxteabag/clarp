// The quick switcher's decisions, kept out of the component so they can be
// tested: what the list shows for a query, and what picking a row means.

// One flat, ordered list. Live chats first — the common case is jumping to a
// conversation that already exists — then contacts, which start a new one.
// Ordering and filtering come from buildAgentOverview so the palette, the
// overview and the rail cannot disagree about what matches a query.
// buildAgentOverview also matches a workspace or a model, which is right for
// the overview's search box and wrong here: you are typing a name. Keep its
// results, but float the name matches — prefix first, then substring — so the
// row you meant is at the top and Enter is safe to hit blind.
function nameRank(name, query) {
  if (!query) return 0;
  const n = String(name || '').toLowerCase();
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  if (n === q) return 0;
  if (n.startsWith(q)) return 1;
  if (n.includes(q)) return 2;
  return 3;
}

export function quickSwitchRows({ chats = [], contacts = [] } = {}, limit = 12, query = '') {
  const rows = [
    ...chats.map(row => ({
      kind: 'chat',
      key: `chat:${row.session}`,
      name: row.name,
      session: row.session,
      detail: row.statusText || (row.busy ? 'Working' : 'Idle'),
      row,
    })),
    ...contacts.map(row => ({
      kind: 'contact',
      key: `contact:${row.name}`,
      name: row.name,
      session: '',
      detail: 'Start a new session',
      row,
    })),
  ];
  // Stable: equal ranks keep the order buildAgentOverview gave them, which is
  // already recency- and status-aware.
  return rows
    .map((row, i) => ({ row, i, rank: nameRank(row.name, query) }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map(entry => entry.row)
    .slice(0, limit);
}

// Picking a row is one of three things, and which one depends on the panes
// that are already open:
//
//   focus   — some pane is already showing this session; go to it rather than
//             retargeting the pane you happen to be in and ending up with the
//             same conversation twice.
//   switch  — the session exists but no pane shows it; the active pane takes it.
//   create  — no session yet; hand the name to the start flow.
export function resolveChoice(row, leaves = [], activePaneId = '') {
  if (!row) return null;
  if (row.kind === 'contact' || !row.session) {
    return { action: 'create', name: row.name };
  }
  const showing = leaves.find(leaf => leaf && leaf.session === row.session);
  if (showing && showing.id !== activePaneId) {
    return { action: 'focus', paneId: showing.id, session: row.session };
  }
  return { action: 'switch', paneId: activePaneId, session: row.session };
}

// Typing past the end of the list should not strand the selection, and an
// empty list has nothing to select.
export function clampIndex(index, length) {
  if (length <= 0) return 0;
  return ((index % length) + length) % length;
}
