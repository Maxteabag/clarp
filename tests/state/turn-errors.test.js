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
import { actionLabel, errorSentence, errorTime } from '../../web/src/lib/live/turn-error-text.js';
import { t } from '../../web/src/stores/i18n.svelte.js';
import en from '../../web/src/lib/i18n/en.json';
import { conversations } from '../../web/src/stores/conversations.svelte.js';
import { errorPlanFor, sessionOfTurn } from '../../web/src/stores/turn-errors.svelte.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(fs.readFileSync(path.join(here, '..', 'fixtures', 'turn-errors.json'), 'utf8'));
const rows = fixture.log_rows;
const row = id => rows.find(r => r.id === id);

describe('turn errors', () => {
  it('a failure row carries its explanation; a plain notice does not', () => {
    const notice = systemNotice(row('marker-u1'));
    expect(notice.explained.code).toBe('invalid_request');
    expect(errorSentence(notice.explained)).toBe('Codex rejected the conversation history. Start a fresh session with the same conversation and try again.');
    expect(systemNotice(row('q1'))).toBeNull();
    expect(explainedError({ reason: 'provider_busy', message: 'API unavailable' })).toBeNull();
  });

  it('has every catalog key of docs/turn-errors.md in English', () => {
    for (const key of ['error.usage_limit', 'error.usage_limit_until', 'error.interrupted', 'error.folder_missing_no_path',
      'error.action.retry', 'error.action.retry_at', 'error.action.copy_details', 'error.details', 'error.copied',
      'error.retry_scheduled', 'error.retry_sent', 'error.action_failed']) {
      expect(typeof en[key], key).toBe('string');
    }
    expect(en['error.repeat_count']).toEqual({ one: '{count}×', other: '{count}×' });
    expect(t('error.action.copy_details')).toBe('Copy diagnostics');
    expect(t('error.copied')).toBe('Diagnostics copied');
  });

  it('falls back to the Host text for an unknown key or an empty placeholder', () => {
    const usage = row('marker-u4').error;
    expect(errorSentence({ ...usage, key: 'error.something_new' }, Date.now(), 'nb')).toBe(usage.text.nb);
    expect(errorSentence({ ...usage, key: 'error.something_new' }, Date.now(), 'fr')).toBe(usage.text.en);
    expect(errorSentence({ ...usage, params: { backend: 'Codex' } })).toBe(usage.text.en);
    const filled = errorSentence(usage, usage.params.time_ms - 3600000);
    expect(filled.startsWith('Your Codex account is out of usage until ')).toBe(true);
  });

  it('formats {time} and the count with Intl', () => {
    const at = new Date(2026, 10, 28, 18, 27).getTime();
    expect(errorTime(at, new Date(2026, 10, 28, 9, 0).getTime(), 'nb')).toBe('18:27');
    expect(errorTime(at, new Date(2026, 10, 25, 9, 0).getTime(), 'en')).toMatch(/^Sat/);
    expect(errorTime(at, new Date(2026, 9, 1, 9, 0).getTime(), 'en')).toMatch(/Nov/);
    expect(t('error.repeat_count', { count: 3 })).toBe('3×');
    expect(t('error.details')).toBe('Details');
    expect(actionLabel({ id: 'copy_details', label: { en: 'x' } })).toBe('Copy diagnostics');
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
    expect([plain.id, actionLabel(plain, at)]).toEqual(['retry', 'Try again']);
    expect(retryBody(usage, 'tt-codex', plain)).toEqual({ session: 'tt-codex', message_id: 'u4' });
  });

  it('the live failure is the newest card', () => {
    const plan = planTurnErrors(rows, { key: 'live', error: fixture.live_turn_op.turn.error });
    expect(plan.actionable).toBe('live');
  });

  it('a card finds its chat and the chat\'s plan by itself', () => {
    conversations.bySession['tt-codex'] = { turns: rows.map(r => ({ ...r })) };
    const turns = conversations.bySession['tt-codex'].turns;
    const card = turns.find(r => r.id === 'marker-u3');
    expect(sessionOfTurn(card)).toBe('tt-codex');
    const plan = errorPlanFor('tt-codex');
    expect([...plan.hidden]).toEqual(['marker-u1', 'marker-u2']);
    expect(plan.counts.get('marker-u3')).toBe(3);
    expect(errorPlanFor('tt-codex')).toBe(plan);
    conversations.bySession['tt-codex'].turns = [...turns, { id: 'a9', role: 'assistant', text: 'Done.' }];
    expect(errorPlanFor('tt-codex').actionable).toBeNull();
    delete conversations.bySession['tt-codex'];
  });
});
