// Pure view-model builder for the responsive Agents control surface.
// Session identity is authoritative: two chats with the same Contact name are
// two rows, while a Contact without a live chat appears once in Contacts.

function text(value) {
  return String(value || '').trim();
}

function personaKey(name) {
  return text(name).toLocaleLowerCase();
}

function matches(row, query) {
  if (!query) return true;
  return [
    row.name, row.session, row.cwd, row.backend, row.model, row.effort,
    row.preview, row.statusText, row.personality,
  ].some(value => text(value).toLocaleLowerCase().includes(query));
}

function sortChats(a, b) {
  if (a.isCurrent !== b.isCurrent) return a.isCurrent ? -1 : 1;
  if (a.busy !== b.busy) return a.busy ? -1 : 1;
  if (a.isUnread !== b.isUnread) return a.isUnread ? -1 : 1;
  if (a.lastActivity !== b.lastActivity) return b.lastActivity - a.lastActivity;
  return a.name.localeCompare(b.name);
}

export function buildAgentOverview({
  agentsBySession = {}, personas = [], roster = [], availableSessions = [],
  currentSession = '', query = '', isUnread = () => false,
} = {}) {
  const normalizedQuery = text(query).toLocaleLowerCase();
  const personaByName = new Map(
    (personas || []).map(persona => [personaKey(persona.name), persona]));
  const activeNames = new Set();
  const chats = [];
  const archived = [];

  for (const [session, agent] of Object.entries(agentsBySession || {})) {
    if (!agent) continue;
    const name = text(agent.name || agent.persona || session) || session;
    const persona = personaByName.get(personaKey(name)) || {};
    activeNames.add(personaKey(name));
    const busy = !!agent.busy;
    const unread = !!isUnread(session);
    const statusText = text(agent.status_text);
    const activity = text(agent.activity_summary || agent.activity?.summary);
    const preview = text(agent.last_message) || activity || text(agent.cwd) || 'No messages yet';
    const row = {
      ...agent,
      key: `chat:${agent.agent_id || session}`,
      session,
      name,
      personality: text(persona.personality),
      builtin: !!persona.builtin,
      isCurrent: session === currentSession,
      isAvailable: availableSessions.includes(session),
      isUnread: unread,
      busy,
      statusText,
      preview,
      lastActivity: Number(agent.last_activity) || 0,
      archived: !!agent.archived_at,
      stateLabel: agent.compacting ? 'compacting'
        : busy ? text(agent.activity_phase || agent.latest_state) || 'working'
        : unread ? 'new reply'
        : statusText || 'idle',
    };
    (row.archived ? archived : chats).push(row);
  }

  const contactSource = personas.length
    ? personas
    : roster.map((name, index) => ({ id: `roster-${index}`, name }));
  const contacts = contactSource
    .filter(persona => !activeNames.has(personaKey(persona.name)))
    .map((persona, index) => ({
      ...persona,
      key: `contact:${persona.id || index}:${persona.name}`,
      name: text(persona.name),
      personality: text(persona.personality),
      builtin: !!persona.builtin,
      archived: false,
    }));

  chats.sort(sortChats);
  archived.sort((a, b) => b.lastActivity - a.lastActivity);

  return {
    chats: chats.filter(row => matches(row, normalizedQuery)),
    contacts: contacts.filter(row => matches(row, normalizedQuery)),
    archived: archived.filter(row => matches(row, normalizedQuery)),
    counts: {
      chats: chats.length,
      working: chats.filter(row => row.busy).length,
      attention: chats.filter(row => row.isUnread || row.statusText).length,
      contacts: contacts.length,
      archived: archived.length,
    },
  };
}

export function formatRelativeActivity(epochSeconds, nowSeconds = Date.now() / 1000) {
  const value = Number(epochSeconds) || 0;
  if (!value) return 'No activity yet';
  const seconds = Math.max(0, Math.floor(nowSeconds - value));
  if (seconds < 45) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value * 1000).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric',
  });
}
