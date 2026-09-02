import { describe, expect, it } from 'vitest';
import {
  normalizeBackend, resumableBackend, supportsForkBackend, catalogBackendIds,
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
