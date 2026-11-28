import { AgentBackend } from './protocol.js';
import { english } from './i18n-en.js';

const ALIASES = {
  antigravity: AgentBackend.AGY,
  'antigravity-acp': AgentBackend.AGY_ACP,
  agy_acp: AgentBackend.AGY_ACP,
  'open-code': AgentBackend.OPENCODE,
  'deep-seek': AgentBackend.DEEPSEEK,
};

export function normalizeBackend(raw) {
  const value = String(raw || '').trim().toLowerCase();
  if (!value) return AgentBackend.CLAUDE;
  return ALIASES[value] || value;
}

export function resumableBackend(raw) {
  return normalizeBackend(raw);
}

// Outgoing selections use ACP. Roster and transcript readers still normalize
// legacy Antigravity to agy so their existing identity stays intact.
export function launchBackend(raw) {
  const backend = normalizeBackend(raw);
  return backend === AgentBackend.AGY ? AgentBackend.AGY_ACP : backend;
}

export function canSelectBackend(raw, catalogue) {
  const backend = launchBackend(raw);
  return backend !== AgentBackend.AGY_ACP
    || providerRow(backend, catalogue)?.available === true;
}

function providerRow(raw, catalogue) {
  const backend = normalizeBackend(raw);
  const provider = catalogue && catalogue.providers && catalogue.providers[backend];
  return provider && typeof provider === 'object' ? provider : null;
}

// A supports_* flag from the catalogue row, or the client-side default when
// the Host predates the flag. Absent never means "no": an old Host must not
// hide a control that worked yesterday.
function flag(raw, catalogue, key, fallback) {
  const provider = providerRow(raw, catalogue);
  if (provider && typeof provider[key] === 'boolean') return provider[key];
  return fallback;
}

export function supportsForkBackend(raw, catalogue) {
  return flag(raw, catalogue, 'supports_fork',
              normalizeBackend(raw) === AgentBackend.CLAUDE);
}

export function supportsResumeBackend(raw, catalogue) {
  const provider = providerRow(raw, catalogue);
  if (normalizeBackend(raw) === AgentBackend.AGY_ACP
      && provider?.past_sessions_backend !== AgentBackend.AGY_ACP) return false;
  if (provider && typeof provider.supports_resume === 'boolean') {
    return provider.supports_resume;
  }
  return flag(raw, catalogue, 'resumable', true);
}

export function supportsCompactBackend(raw, catalogue) {
  return flag(raw, catalogue, 'supports_compact', true);
}

export function supportsMcpBackend(raw, catalogue) {
  return flag(raw, catalogue, 'supports_mcp',
              normalizeBackend(raw) === AgentBackend.CLAUDE);
}

export function supportsSteerBackend(raw, catalogue) {
  return flag(raw, catalogue, 'supports_steer', false);
}

// "picker" | "hidden" | "folded_into_model"; the Host says which control the
// effort field gets, and effort_help is the note shown instead of a picker.
export function effortUI(raw, catalogue, tr = english) {
  const provider = providerRow(raw, catalogue);
  const ui = provider && provider.effort_ui;
  if (ui === 'hidden' || ui === 'folded_into_model' || ui === 'picker') {
    return { ui, help: String(provider.effort_help || '') };
  }
  if (provider && provider.model_effort_compatibility === 'unknown') {
    return { ui: 'folded_into_model', help: tr('launch.effort_not_reported') };
  }
  return { ui: 'picker', help: '' };
}

export function backendLabel(raw, catalogue) {
  const backend = normalizeBackend(raw);
  const provider = providerRow(backend, catalogue);
  if (provider && provider.label) return provider.label;
  if (backend === AgentBackend.CLAUDE) return 'Claude';
  if (backend === AgentBackend.CODEX) return 'Codex';
  if (backend === AgentBackend.AGY || backend === AgentBackend.AGY_ACP) return 'Antigravity';
  if (backend === AgentBackend.GROK) return 'Grok';
  if (backend === AgentBackend.OPENCODE) return 'OpenCode';
  if (backend === AgentBackend.DEEPSEEK) return 'DeepSeek';
  return backend;
}

export function backendDetail(raw, catalogue, tr = english) {
  const provider = providerRow(raw, catalogue);
  if (provider && provider.detail) return provider.detail;
  return tr('launch.runs_on', { backend: backendLabel(raw, catalogue) });
}

const BUNDLED_IDS = [AgentBackend.CLAUDE, AgentBackend.CODEX,
                     AgentBackend.GROK, AgentBackend.OPENCODE, AgentBackend.DEEPSEEK,
                     AgentBackend.MUSE];

// The backends a chooser offers: catalogue rows that are installed on this
// Host and not hidden, plus `current` (the backend an existing agent already
// runs on, so a missing CLI does not vanish mid-chat), in sort_index order.
// Never empty: with nothing installed every row is offered rather than none.
export function catalogBackendIds(catalogue, { current = '' } = {}) {
  const providers = (catalogue && catalogue.providers) || {};
  const all = Object.keys(providers).filter(
    id => id && id !== AgentBackend.AGY && canSelectBackend(id, catalogue),
  );
  if (!all.length) return BUNDLED_IDS.slice();
  const keep = current ? normalizeBackend(current) : '';
  const offered = all.filter(id => {
    const row = providers[id] || {};
    if (id === keep) return true;
    if (row.installed === false) return false;
    return row.hidden !== true;
  });
  const ids = offered.length ? offered : all;
  const order = id => {
    const v = providers[id] && providers[id].sort_index;
    return Number.isFinite(v) ? v : Number.POSITIVE_INFINITY;
  };
  return ids
    .map((id, index) => ({ id, index }))
    .sort((a, b) => order(a.id) - order(b.id) || a.index - b.index)
    .map(item => item.id);
}

export const CONTACT_TIERS = {
  // Claude Tier
  claude: 'claude', 'claude default': 'claude', 'default claude': 'claude',
  mike: 'claude', rachel: 'claude', domi: 'claude', bella: 'claude',
  antoni: 'claude', elli: 'claude', josh: 'claude', arnold: 'claude',
  adam: 'claude', sam: 'claude', marcus: 'claude', caleb: 'claude',
  nadia: 'claude', priya: 'claude', diego: 'claude', lena: 'claude',
  theo: 'claude', yuki: 'claude', omar: 'claude', freya: 'claude',
  archie: 'claude', tristan: 'claude', ronan: 'claude', wyatt: 'claude',
  jasper: 'claude', siobhan: 'claude', bronte: 'claude', pearl: 'claude',
  imogen: 'claude', kira: 'claude',

  // Codex Tier
  codex: 'codex', 'codex default': 'codex', 'default codex': 'codex',
  axel: 'codex', iris: 'codex', felix: 'codex', gordon: 'codex',
  quinn: 'codex', marsy: 'codex', silas: 'codex', tigo: 'codex',
  wren: 'codex', kaelen: 'codex', lezo: 'codex', vesper: 'codex',
  nyx: 'codex', avana: 'codex', aura: 'codex', orion: 'codex',
  cipher: 'codex', solon: 'codex', spectra: 'codex', mirai: 'codex',
  afria: 'codex', solu: 'codex', lyra: 'codex', echo: 'codex', nova: 'codex',

  // Grok Tier
  grok: 'grok', 'grok default': 'grok', 'default grok': 'grok',
  margrok: 'grok', roxy: 'grok', vance: 'grok', fang: 'grok',
  riot: 'grok', raven: 'grok', jax: 'grok', dagger: 'grok',
  blaze: 'grok', spike: 'grok',

  // Gemini Tier
  gemini: 'gemini', 'gemini default': 'gemini', 'default gemini': 'gemini',
  pip: 'gemini', bloop: 'gemini', noodle: 'gemini', ziggy: 'gemini', mochi: 'gemini',

  // DeepSeek Tier
  deepseek: 'deepseek', 'deepseek default': 'deepseek', 'default deepseek': 'deepseek',
  fathom: 'deepseek', coral: 'deepseek', nautilus: 'deepseek', luma: 'deepseek', trench: 'deepseek',

  // Janitor Tier
  janitor: 'janitor', 'janitor default': 'janitor', 'default janitor': 'janitor',
  rivet: 'janitor', 'paper cuts man': 'janitor', papercutsman: 'janitor',
  clank: 'janitor', rusty: 'janitor', gearbox: 'janitor', sprocket: 'janitor',
  scrappy: 'janitor', bolts: 'janitor', duster: 'janitor', valve: 'janitor',
};

export const TIER_BACKENDS = {
  claude: AgentBackend.CLAUDE,
  codex: AgentBackend.CODEX,
  grok: AgentBackend.GROK,
  gemini: AgentBackend.AGY,
  deepseek: AgentBackend.DEEPSEEK,
  janitor: AgentBackend.CODEX,
  janitors: AgentBackend.CODEX,
};

export function tierForContact(name, persona = null) {
  if (persona && persona.tier) return String(persona.tier).trim().toLowerCase();
  const key = String(name || '').trim().toLowerCase();
  return CONTACT_TIERS[key] || null;
}

export function allowedBackendForContact(name, persona = null) {
  const tier = tierForContact(name, persona);
  if (!tier) return null;
  return TIER_BACKENDS[tier] || null;
}

export function isContactBackendAllowed(name, backend, persona = null) {
  const allowed = allowedBackendForContact(name, persona);
  if (!allowed) return true;
  return normalizeBackend(backend) === normalizeBackend(allowed);
}

