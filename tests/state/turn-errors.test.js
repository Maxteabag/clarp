// The Host's turn errors (docs/turn-errors.md, contract 217) in the web app,
// against the contract fixture (tests/fixtures/turn-errors.json, a copy of
// contract/fixtures/turn-errors.json): decoding, language, retry bodies and
// the collapse of repeats.

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  errorActions, explainedError, planTurnErrors, retryBody, systemNotice,
} from '@core/live-present.js';
import {
  actionLabel, appLanguage, errorSentence, formatTime, repeatCount, tr,
} from '@core/turn-error-text.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', 'turn-errors.json'), 'utf8'));
const rows = fixture.log_rows;
const row = id => rows.find(r => r.id === id);

describe('turn errors', () => {
  it('a failure row carries its explanation; a plain notice does not', () => {
    const notice = systemNotice(row('marker-u1'));
    expect(notice.explained.code).toBe('invalid_request');
    expect(errorSentence(notice.explained, 'en')).toBe('Codex rejected the conversation history. Start a fresh session with the same conversation and try again.');
    expect(systemNotice(row('q1'))).toBeNull();
    expect(explainedError({ reason: 'provider_busy', message: 'API unavailable' })).toBeNull();
  });

  it('maps the browser language onto the shipped ones', () => {
    for (const [tag, lang] of [['nb-NO', 'nb'], ['nn', 'nb'], ['no', 'nb'], ['zh-CN', 'zh-Hans'], ['zh-Hans', 'zh-Hans'], ['zh-TW', 'en'],
      ['hi-IN', 'hi'], ['es-MX', 'es'], ['fr', 'fr'], ['sv', 'en'], ['', 'en']]) {
      expect(appLanguage([tag]), tag).toBe(lang);
    }
  });

  it('falls back to the Host text for an unknown key or an empty placeholder', () => {
    const usage = row('marker-u4').error;
    expect(errorSentence({ ...usage, key: 'error.something_new' }, 'nb')).toBe(usage.text.nb);
    expect(errorSentence({ ...usage, key: 'error.something_new' }, 'fr')).toBe(usage.text.en);
    expect(errorSentence({ ...usage, params: { backend: 'Codex' } }, 'en')).toBe(usage.text.en);
    const filled = errorSentence(usage, 'en', usage.params.time_ms - 3600000);
    expect(filled.startsWith('Your Codex account is out of usage until ')).toBe(true);
  });

  it('formats {time} and the count with Intl', () => {
    const at = new Date(2026, 10, 28, 18, 27).getTime();
    expect(formatTime(at, 'nb', new Date(2026, 10, 28, 9, 0).getTime())).toBe('18:27');
    expect(formatTime(at, 'en', new Date(2026, 10, 25, 9, 0).getTime())).toMatch(/^Sat/);
    expect(formatTime(at, 'en', new Date(2026, 9, 1, 9, 0).getTime())).toMatch(/Nov/);
    expect(repeatCount(3, 'en')).toBe('3×');
    expect(tr('error.details', 'en')).toBe('Details');
    expect(actionLabel({ id: 'copy_details', label: { en: 'x' } }, 'en')).toBe('Copy diagnostics');
  });

  it('builds the contract retry bodies and skips unknown actions', () => {
    const fresh = row('marker-u3').error;
    expect(retryBody(fresh, 'tt-codex', fresh.actions[0])).toEqual(fixture.turn_retry.request_fresh);
    const usage = row('marker-u4').error;
    expect(retryBody(usage, 'tt-codex', usage.actions[0])).toEqual(fixture.turn_retry.request_at);
    const long = row('marker-u6').error;
    expect(retryBody(long, 'tt-claude', long.actions[0])).toEqual(fixture.turn_retry.request_compact);
    expect(errorActions(row('marker-u5').error).map(a => a.id)).toEqual(['retry', 'copy_details']);
  });

  it('collapses the three repeats to the newest; only the newest failure offers a retry', () => {
    const plan = planTurnErrors(rows);
    expect([...plan.hidden]).toEqual(['marker-u1', 'marker-u2']);
    expect(plan.counts.get('marker-u3')).toBe(3);
    expect(plan.actionable).toBe('marker-u6');
    const replied = [...rows.slice(0, 3), { id: 'a1', role: 'assistant', text: 'Fixed.' }, ...rows.slice(3), { id: 'a2', role: 'assistant', text: 'Done.' }];
    const apart = planTurnErrors(replied);
    expect([...apart.hidden]).toEqual(['marker-u2']);
    expect(apart.counts.get('marker-u3')).toBe(2);
    expect(apart.actionable).toBeNull();
  });

  it('a retry_at whose time has passed is a plain retry without at_ms', () => {
    const usage = row('marker-u4').error;
    const at = usage.actions[0].at_ms;
    expect(errorActions(usage, at - 1)[0].id).toBe('retry_at');
    const [plain] = errorActions(usage, at);
    expect([plain.id, actionLabel(plain, 'en', at)]).toEqual(['retry', 'Try again']);
    expect(retryBody(usage, 'tt-codex', plain)).toEqual({ session: 'tt-codex', message_id: 'u4' });
  });

  it('the live failure is the newest card', () => {
    const plan = planTurnErrors(rows, { key: 'live', error: fixture.live_turn_op.turn.error });
    expect(plan.actionable).toBe('live');
  });
});
