// The web app's native form reader (static/lib/render-spec.js, issue 428): the catalog rules,
// the answers it sends, the bytes the Host hashes, and where a card sits in the transcript.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  acceptedSubmission, canonical, carrying, clientHeader, footnote, held, matchesIdentity,
  newSubmissionId, readRenderSpec, refusalMeaning, submission, withArtifacts,
} from '../../static/lib/render-spec.js';

const fixture = () => JSON.parse(readFileSync(new URL('../fixtures/render_spec/build-decision.json', import.meta.url), 'utf8'));
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

describe('reading clarp.form v1', () => {
  it('reads the build decision fixture into a form', () => {
    const reading = readRenderSpec(fixture());
    expect(reading.kind).toBe('form');
    const { form } = reading;
    expect(form.root.type).toBe('stack');
    expect(form.root.children.map(c => c.type)).toEqual(['heading', 'text', 'choice', 'text_field', 'toggle']);
    expect(form.submitLabel).toBe('Submit');
    expect([...form.required]).toEqual(['choice']);
    expect(form.initial).toEqual({ notify: false });  // a toggle without a value starts false
  });

  it('says newer for a catalog version, component or property it does not know', () => {
    expect(readRenderSpec({ ...fixture(), catalog_version: 3 }).kind).toBe('newer');
    const unknownType = fixture();
    unknownType.spec.elements.topic.type = 'chart';
    expect(readRenderSpec(unknownType).kind).toBe('newer');
    const unknownProp = fixture();
    unknownProp.spec.elements.topic.props.colour = 'red';
    expect(readRenderSpec(unknownProp).kind).toBe('newer');
  });

  it('says unreadable, with the place, for a form broken against the catalog', () => {
    const shared = fixture();
    shared.spec.elements.root.children.push('topic');
    expect(readRenderSpec(shared)).toMatchObject({ kind: 'unreadable' });
    const badOptions = fixture();
    badOptions.spec.elements.selection.props.options.reverse();  // enum order differs
    expect(readRenderSpec(badOptions)).toEqual({ kind: 'unreadable', pointer: '/spec/elements/selection/props/value' });
    const nested = fixture();
    nested.spec.elements.root.type = 'card';
    nested.spec.elements.topic = { type: 'card', props: {}, children: [] };
    expect(readRenderSpec(nested).kind).toBe('unreadable');
  });
});

describe('the answers it sends', () => {
  const { form } = readRenderSpec(fixture());

  it('holds Submit until a required value is there, and says why', () => {
    const result = submission(form, form.initial);
    expect(result).toEqual({ ready: false, gaps: [{ gap: 'missing', key: 'choice' }] });
    expect(footnote({ form, values: form.initial })).toBe('One required answer is missing.');
  });

  it('leaves out an empty text and keeps every toggle', () => {
    const result = submission(form, { ...form.initial, choice: 'send', answer: '' });
    expect(result).toEqual({ ready: true, answers: { intent: 'submit_decision', state: { choice: 'send', notify: false } } });
  });

  it('measures text in code points and keeps it within maxLength', () => {
    expect(held(form, 'answer', '😀'.repeat(2001))).toBe('😀'.repeat(2000));
  });

  it('carries typed values into a new version only where the field still takes them', () => {
    expect(carrying(form, { choice: 'nope', answer: 'hi', notify: true, stray: 1 })).toEqual({ notify: true, answer: 'hi' });
  });
});

describe('the bytes the Host hashes', () => {
  it('writes canonical JSON exactly as the Host does', () => {
    const answers = { intent: 'submit_decision', state: { notify: false, choice: 'send', answer: 'Ship æ ø 😀 "q"\n' } };
    expect(new TextDecoder().decode(canonical(answers)))
      .toBe('{"intent":"submit_decision","state":{"answer":"Ship æ ø 😀 \\"q\\"\\n","choice":"send","notify":false}}');
  });

  it('matches the content identity the Host computes for the fixture', async () => {
    const { form } = readRenderSpec(fixture());
    // render_spec.content_identity(spec, version) for tests/fixtures/render_spec/build-decision.json
    const identity = { version: 'build-1', bytes: 729, sha256: 'c1:f746ceefe6aa1dca21eac2e598719a3a4441a3c3c37aedfc448f351b20636647' };
    expect(await matchesIdentity(form, identity, sha)).toBe(true);
    expect(await matchesIdentity(form, { ...identity, sha256: 'c1:00' }, sha)).toBe(false);
    expect(await matchesIdentity(form, { ...identity, bytes: 1 }, null)).toBe(false);
  });
});

describe('the rest of the form path', () => {
  it('names the client in the Host\'s strict header form', () => {
    expect(clientHeader('0.5.3')).toBe('web/0.5.3 contract=2');
    expect(clientHeader('a b/c')).toBe('web/abc contract=2');
  });

  it('makes submission ids the Host takes', () => {
    const id = newSubmissionId();
    expect(id).toMatch(/^[A-Za-z0-9-]{16,80}$/);
    expect(newSubmissionId()).not.toBe(id);
  });

  it('reads the accepted submission of an answered form', () => {
    const accepted = acceptedSubmission({
      submission_id: 'abcdefabcdefabcdef', version: 'build-1', accepted_at: 1, delivery_status: 'delivered',
      answers: { intent: 'submit_decision', state: { choice: 'send', notify: true } },
    });
    expect(accepted.state).toEqual({ choice: 'send', notify: true });
    expect(acceptedSubmission({ submission_id: 'x' })).toBe(null);
  });

  it('keys refusals on the code', () => {
    expect(refusalMeaning('version_mismatch')).toBe('reopen');
    expect(refusalMeaning('not_open')).toBe('closed');
    expect(refusalMeaning('client_contract')).toBe('too_old');
    expect(refusalMeaning('whatever')).toBe('other');
  });
});

describe('placing a card in the transcript', () => {
  const turn = (id, iso) => ({ type: 'turn', key: 't:' + id, item: { id, timestamp: iso } });
  const row = (id, ms) => ({ artifact_id: id, created_at: ms });
  const t = iso => Date.parse(iso);
  const entries = [turn('a', '2026-11-22T10:00:00Z'), turn('b', '2026-11-22T10:05:00Z')];

  it('sits before the first entry written after it was published', () => {
    const out = withArtifacts(entries, [row('f', t('2026-11-22T10:01:00Z'))]);
    expect(out.map(e => e.key)).toEqual(['t:a', 'r:f', 't:b']);
  });

  it('goes last when it is the newest, and after a live turn that started before it', () => {
    expect(withArtifacts(entries, [row('f', t('2026-11-22T10:09:00Z'))]).map(e => e.key)).toEqual(['t:a', 't:b', 'r:f']);
    const live = [...entries, { type: 'live', key: 'live-turn' }];
    const out = withArtifacts(live, [row('f', t('2026-11-22T10:09:00Z'))], { liveStartMs: t('2026-11-22T10:06:00Z') });
    expect(out.map(e => e.key)).toEqual(['t:a', 't:b', 'live-turn', 'r:f']);
  });

  it('waits for an older page unless the chat has none', () => {
    const old = [row('f', t('2026-11-22T09:00:00Z'))];
    expect(withArtifacts(entries, old).map(e => e.key)).toEqual(['t:a', 't:b']);
    expect(withArtifacts(entries, old, { complete: true }).map(e => e.key)).toEqual(['r:f', 't:a', 't:b']);
    expect(withArtifacts([], old).map(e => e.key)).toEqual(['r:f']);
  });
});
