// The chat list's line of a message from the Host's preview part (feature chat_preview_parts,
// Host contract 217, contract/fixtures/chat-preview.json): a kind and its values, worded here in
// the app's language (`tr`, default English). Null for a missing or unknown kind, so the caller
// shows the Host's own English line (`last_message` / `preview`).

import { english } from './i18n-en.js';

const OUTCOMES = new Set(['approved', 'declined', 'discarded', 'expired', 'resolved']);
const TOOLS = new Set(['exec', 'read', 'list', 'search', 'edit', 'write', 'fetch', 'mcp', 'agent', 'todo']);

const str = v => String(v ?? '').trim();

function line(part, tr) {
  switch (part.kind) {
    case 'text': return str(part.text);
    case 'image': return str(part.caption) ? tr('preview.image_caption', { caption: str(part.caption) }) : tr('preview.image');
    case 'images': return tr('preview.images', { count: Number(part.count) || 0 });
    case 'file': return tr('preview.file', { name: str(part.name) });
    case 'files': return tr('preview.files', { count: Number(part.count) || 0 });
    case 'attachments': return tr('preview.attachments', { count: Number(part.count) || 0 });
    case 'code': return tr('preview.code');
    case 'decision': {
      const outcome = part.outcome === 'answered' ? str(part.answer)
        : tr(`preview.decision.${OUTCOMES.has(part.outcome) ? part.outcome : 'resolved'}`);
      const question = str(part.question);
      return question ? tr('preview.decision', { question, outcome }) : outcome;
    }
    case 'tool': {
      if (part.category === 'todo') return tr('preview.tool.todo');
      if (!TOOLS.has(part.category)) return tr('preview.tool.other', { name: str(part.name) }).trim();
      return tr(`preview.tool.${part.category}`, { label: str(part.label) }).trim();
    }
    default: return null;
  }
}

/** The line for a preview part, "You: " before the user's own; null when the kind is unknown. */
export function previewLine(part, tr = english) {
  if (!part || typeof part !== 'object') return null;
  const text = line(part, tr);
  if (text == null) return null;
  return part.own ? tr('preview.own', { text }) : text;
}
