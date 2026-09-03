import { describe, expect, it } from 'vitest';
import {
  normalizeBackend, resumableBackend, supportsForkBackend, catalogBackendIds,
  supportsResumeBackend, supportsCompactBackend, supportsMcpBackend,
  supportsSteerBackend, effortUI, backendLabel, backendDetail,
} from '../../static/lib/agent-launch.js';

describe('agent launch policy', () => {
  it('keeps unknown backend ids instead of rewriting them to claude', () => {
    expect(normalizeBackend('codex')).toBe('codex');
    expect(normalizeBackend('grok')).toBe('grok');
    expect(normalizeBackend('opencode')).toBe('opencode');
    expect(normalizeBackend('future-cli')).toBe('future-cli');
    expect(normalizeBackend('unknown')).toBe('unknown');
    expect(normalizeBackend('')).toBe('claude');
  });

  it('passes every backend id through for resume', () => {
    expect(resumableBackend('agy')).toBe('agy');
    expect(resumableBackend('codex')).toBe('codex');
    expect(resumableBackend('grok')).toBe('grok');
    expect(resumableBackend('opencode')).toBe('opencode');
    expect(resumableBackend('future-cli')).toBe('future-cli');
  });

  it('allows forks only for the backend with safe copy semantics', () => {
    expect(supportsForkBackend('claude')).toBe(true);
    expect(supportsForkBackend('codex')).toBe(false);
    expect(supportsForkBackend('agy')).toBe(false);
    expect(supportsForkBackend('grok')).toBe(false);
    const catalogue = { providers: { 'future-cli': { supports_fork: true } } };
    expect(supportsForkBackend('future-cli', catalogue)).toBe(true);
  });

  it('lists catalogue providers when the Host advertises them', () => {
    expect(catalogBackendIds({
      providers: { grok: { id: 'grok' }, claude: { id: 'claude' } },
    })).toEqual(['grok', 'claude']);
  });
});

describe('catalogue-driven chooser', () => {
  const catalogue = {
    providers: {
      opencode: { id: 'opencode', installed: true, sort_index: 4 },
      claude: { id: 'claude', installed: true, sort_index: 0 },
      codex: { id: 'codex', installed: false, sort_index: 1 },
      agy: { id: 'agy', installed: true, hidden: true, sort_index: 2 },
      grok: { id: 'grok', installed: true, sort_index: 3 },
    },
  };

  it('offers installed, unhidden providers in sort order', () => {
    expect(catalogBackendIds(catalogue)).toEqual(['claude', 'grok', 'opencode']);
  });

  it('keeps the backend an existing agent runs on even when its CLI is gone', () => {
    expect(catalogBackendIds(catalogue, { current: 'codex' }))
      .toEqual(['claude', 'codex', 'grok', 'opencode']);
  });

  it('never returns an empty chooser', () => {
    const nothingInstalled = {
      providers: { grok: { installed: false }, claude: { installed: false } },
    };
    expect(catalogBackendIds(nothingInstalled)).toEqual(['grok', 'claude']);
    expect(catalogBackendIds(null)).toEqual(['claude', 'codex', 'agy', 'grok', 'opencode']);
  });

  it('treats a missing flag as the old-Host default, never as "no"', () => {
    const old = { providers: { opencode: { id: 'opencode' } } };
    expect(supportsResumeBackend('opencode', old)).toBe(true);
    expect(supportsCompactBackend('opencode', old)).toBe(true);
    expect(supportsMcpBackend('opencode', old)).toBe(false);
    expect(supportsMcpBackend('claude', old)).toBe(true);
    expect(supportsSteerBackend('codex', old)).toBe(false);
    const host = { providers: {
      opencode: { supports_compact: false, supports_resume: false },
      codex: { supports_steer: true },
      'future-cli': { supports_mcp: true },
    } };
    expect(supportsResumeBackend('opencode', host)).toBe(false);
    expect(supportsCompactBackend('opencode', host)).toBe(false);
    expect(supportsSteerBackend('codex', host)).toBe(true);
    expect(supportsMcpBackend('future-cli', host)).toBe(true);
  });

  it('lets the Host decide the effort control', () => {
    const host = { providers: {
      agy: { effort_ui: 'folded_into_model', effort_help: 'Included in model choice' },
      old: { model_effort_compatibility: 'unknown' },
      grok: {},
    } };
    expect(effortUI('agy', host)).toEqual({ ui: 'folded_into_model', help: 'Included in model choice' });
    expect(effortUI('old', host)).toEqual({ ui: 'folded_into_model', help: 'Not separately reported' });
    expect(effortUI('grok', host)).toEqual({ ui: 'picker', help: '' });
  });

  it('labels and details prefer the catalogue and fall back to the id', () => {
    const host = { providers: { 'future-cli': { label: 'Future', detail: 'Runs on Future CLI.' } } };
    expect(backendLabel('future-cli', host)).toBe('Future');
    expect(backendDetail('future-cli', host)).toBe('Runs on Future CLI.');
    expect(backendLabel('future-cli')).toBe('future-cli');
    expect(backendDetail('grok')).toBe('Runs on Grok.');
  });
});
