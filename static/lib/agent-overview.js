// Pure view-model builder for the responsive Agents control surface.
// Session identity is authoritative: two chats with the same Contact name are
// two rows, while a Contact without a live chat appears once in Contacts.
// Words come from the web app's catalog through `tr` (default English; web/docs/i18n.md).

import { english } from './i18n-en.js';
import { previewLine } from './chat-preview.js';

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

/** Agent states a chat card names in words of its own (overview.state.<state>); others show as sent. */
const STATES = new Set(['thinking', 'responding', 'tool', 'compacting', 'working', 'idle']);

function sortChats(a, b) {
  if (a.isCurrent !== b.isCurrent) return a.isCurrent ? -1 : 1;
  if (a.busy !== b.busy) return a.busy ? -1 : 1;
  if (a.isUnread !== b.isUnread) return a.isUnread ? -1 : 1;
  if (a.lastActivity !== b.lastActivity) return b.lastActivity - a.lastActivity;
  return a.name.localeCompare(b.name);
}

export function buildAgentOverview({
  agentsBySession = {}, personas = [], roster = [], availableSessions = [],
  currentSession = '', query = '', isUnread = () => false, tr = english,
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
    if (agent.is_janitor || agent.interaction_capabilities?.can_chat === false) continue;
    const busy = !!agent.busy;
    const unread = !!isUnread(session);
    const statusText = text(agent.status_text);
    const activity = text(agent.activity_summary || agent.activity?.summary);
    const preview = text(previewLine(agent.last_message_preview, tr) ?? agent.last_message)
      || activity || text(agent.cwd) || tr('overview.no_messages');
    const phase = text(agent.activity_phase || agent.latest_state);
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
      stateLabel: agent.compacting ? tr('overview.state.compacting')
        : busy ? (STATES.has(phase) ? tr(`overview.state.${phase}`) : phase || tr('overview.state.working'))
        : unread ? tr('overview.state.new_reply')
        : statusText || tr('overview.state.idle'),
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

/**
 * When a chat was last active: `relative(ms, nowMs)` (the app passes format.js formatRelative)
 * for a time, the catalog's "No activity yet" for none.
 */
export function formatRelativeActivity(epochSeconds, nowSeconds = Date.now() / 1000,
  { tr = english, relative = englishRelative } = {}) {
  const value = Number(epochSeconds) || 0;
  if (!value) return tr('overview.no_activity');
  return relative(value * 1000, nowSeconds * 1000);
}

function englishRelative(ms, nowMs) {
  const secs = Math.round((ms - nowMs) / 1000);
  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  if (Math.abs(secs) < 45) return rtf.format(0, 'second');
  if (Math.abs(secs) < 3600) return rtf.format(Math.trunc(secs / 60), 'minute');
  if (Math.abs(secs) < 86400) return rtf.format(Math.trunc(secs / 3600), 'hour');
  return rtf.format(Math.trunc(secs / 86400), 'day');
}
