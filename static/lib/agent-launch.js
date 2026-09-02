import { AgentBackend } from './protocol.js';

const ALIASES = { antigravity: AgentBackend.AGY, 'open-code': AgentBackend.OPENCODE };

export function normalizeBackend(raw) {
  const value = String(raw || '').trim().toLowerCase();
  if (!value) return AgentBackend.CLAUDE;
  return ALIASES[value] || value;
}

export function resumableBackend(raw) {
  return normalizeBackend(raw);
}

export function supportsForkBackend(raw, catalogue) {
  const backend = normalizeBackend(raw);
  const provider = catalogue && catalogue.providers && catalogue.providers[backend];
  if (provider && typeof provider.supports_fork === 'boolean') {
    return provider.supports_fork;
  }
  return backend === AgentBackend.CLAUDE;
}

export function backendLabel(raw, catalogue) {
  const backend = normalizeBackend(raw);
  const provider = catalogue && catalogue.providers && catalogue.providers[backend];
  if (provider && provider.label) return provider.label;
  if (backend === AgentBackend.CLAUDE) return 'Claude';
  if (backend === AgentBackend.CODEX) return 'Codex';
  if (backend === AgentBackend.AGY) return 'Antigravity';
  if (backend === AgentBackend.GROK) return 'Grok';
  if (backend === AgentBackend.OPENCODE) return 'OpenCode';
  return backend;
}

export function catalogBackendIds(catalogue) {
  const providers = (catalogue && catalogue.providers) || {};
  const ids = Object.keys(providers).filter(Boolean);
  if (ids.length) return ids;
  return [AgentBackend.CLAUDE, AgentBackend.CODEX, AgentBackend.AGY,
          AgentBackend.GROK, AgentBackend.OPENCODE];
}
