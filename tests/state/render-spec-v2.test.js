// clarp.form version 2 in the web app's reader (static/lib/render-spec.js, issue 673): chart,
// table and button read as the Host's validator checks them, a button form's answer, and
// integers drawn with `decimals`.
import { describe, expect, it } from 'vitest';
import { answeredLine, formatScaled, readRenderSpec, submission } from '../../static/lib/render-spec.js';

// The example form of the v2 agreement: a chart, a table, a note and two buttons.
const example = () => ({
  version: 'deploy-7', catalog: 'clarp.form', catalog_version: 2,
  answer_schema: { type: 'object', properties: {
    action: { type: 'string', enum: ['ship', 'hold'] },
    note: { type: 'string', maxLength: 500 } }, required: ['action'] },
  spec: { root: 'root', state: {}, elements: {
    root: { type: 'stack', props: {}, children: ['lat', 'cmp', 'note', 'row'] },
    lat: { type: 'chart', props: { kind: 'line', title: 'p95 latency', unit: 'ms', decimals: 1,
      series: [{ name: 'before', points: [{ x: 'Mon', y: 1200 }, { x: 'Tue', y: 1185 }] },
               { name: 'after', points: [{ x: 'Mon', y: 800 }, { x: 'Tue', y: 770 }] }] }, children: [] },
    cmp: { type: 'table', props: { columns: [{ key: 'host', label: 'Host' }, { key: 'ok', label: 'Healthy', align: 'end' }],
      rows: [{ host: 'one', ok: true }, { host: 'two', ok: false }] }, children: [] },
    note: { type: 'text_field', props: { label: 'Note', value: { $bindState: '/note' } }, children: [] },
    row: { type: 'stack', props: { axis: 'horizontal' }, children: ['ship', 'hold'] },
    ship: { type: 'button', props: { label: 'Ship it', action: 'ship', style: 'primary' }, children: [] },
    hold: { type: 'button', props: { label: 'Hold', action: 'hold', style: 'destructive', confirm: 'Hold the release?' }, children: [] },
  } },
});
const chart = (props) => {
  const f = example();
  Object.assign(f.spec.elements.lat.props, props);
  return readRenderSpec(f);
};

describe('reading clarp.form v2', () => {
  it('reads the agreed example into a button form', () => {
    const r = readRenderSpec(example());
    expect(r.kind).toBe('form');
    expect(r.form.buttons.map(b => [b.action, b.style, b.confirm])).toEqual([
      ['ship', 'primary', null], ['hold', 'destructive', 'Hold the release?']]);
    expect(r.form.submitIntent).toBe('button');
    expect(r.form.submitLabel).toBe(null);
    const [lat, cmp] = r.form.root.children;
    expect(lat.props).toMatchObject({ kind: 'line', decimals: 1, numericX: false });
    expect(cmp.props.columns.map(c => [c.key, c.align, c.decimals])).toEqual([['host', 'start', 0], ['ok', 'end', 0]]);
  });

  it('says newer for a v2 type in a v1 form', () => {
    expect(readRenderSpec({ ...example(), catalog_version: 1 }).kind).toBe('newer');
  });

  it('refuses a submit beside buttons, and requires one without them', () => {
    expect(readRenderSpec({ ...example(), submit: { label: 'Go', intent: 'go' } })).toEqual({ kind: 'unreadable', pointer: '/submit' });
    const plain = example();
    plain.spec.elements.row.children = [];
    delete plain.spec.elements.ship; delete plain.spec.elements.hold;
    delete plain.answer_schema.properties.action; plain.answer_schema.required = [];
    expect(readRenderSpec(plain)).toEqual({ kind: 'unreadable', pointer: '/submit' });
    expect(readRenderSpec({ ...plain, submit: { label: 'Go', intent: 'go' } }).kind).toBe('form');
  });

  it('holds the action schema to the buttons', () => {
    const extra = example();
    extra.answer_schema.properties.action.enum.push('later');
    expect(readRenderSpec(extra)).toEqual({ kind: 'unreadable', pointer: '/answer_schema/properties/action' });
    const optional = example();
    optional.answer_schema.required = [];
    expect(readRenderSpec(optional).kind).toBe('unreadable');
    const twice = example();
    twice.spec.elements.hold.props.action = 'ship';
    expect(readRenderSpec(twice).kind).toBe('unreadable');
  });

  it('checks a chart as the Host does', () => {
    const twin = [{ name: 'a', points: [{ x: 'Mon', y: 1 }] }, { name: 'a', points: [{ x: 'Mon', y: 2 }] }];
    expect(chart({ series: twin })).toEqual({ kind: 'unreadable', pointer: '/spec/elements/lat/props/series/1/name' });
    expect(chart({ series: [{ name: 'a', points: [{ x: 'Mon', y: 1 }, { x: 2, y: 1 }] }] }).kind).toBe('unreadable');  // x mixed
    expect(chart({ series: [{ name: 'a', points: [{ x: 'Mon', y: 1 }, { x: 'Mon', y: 2 }] }] }).kind).toBe('unreadable');  // x twice
    expect(chart({ series: [{ name: 'a', points: [{ x: 2, y: 1 }, { x: 1, y: 2 }] }] }).kind).toBe('unreadable');  // x falls
    expect(chart({ series: [{ name: 'a', points: [{ x: 'a', y: 1.5 }] }] }).kind).toBe('unreadable');  // a fraction
    expect(chart({ kind: 'pie' }).kind).toBe('unreadable');  // two series
    expect(chart({ kind: 'pie', series: [{ name: 'a', points: [{ x: 'a', y: -1 }] }] }).kind).toBe('unreadable');
    const many = Array.from({ length: 61 }, (_, i) => ({ x: i, y: i }));
    expect(chart({ kind: 'bar', series: [{ name: 'a', points: many }] }).kind).toBe('unreadable');
    expect(chart({ kind: 'line', series: [{ name: 'a', points: many }] }).form.root.children[0].props.numericX).toBe(true);
    expect(chart({ decimals: 7 }).kind).toBe('unreadable');
  });

  it('checks a table as the Host does', () => {
    const table = (props) => { const f = example(); Object.assign(f.spec.elements.cmp.props, props); return readRenderSpec(f); };
    expect(table({ rows: [{ host: 'one', stray: 1 }] }).kind).toBe('unreadable');
    expect(table({ rows: [{ host: 'x'.repeat(201) }] }).kind).toBe('unreadable');
    expect(table({ columns: [{ key: 'host', label: 'A' }, { key: 'host', label: 'B' }] }).kind).toBe('unreadable');
    expect(table({ rows: Array.from({ length: 200 }, () => ({ host: null })) }).kind).toBe('form');
    const wide = Array.from({ length: 8 }, (_, i) => ({ key: 'c' + i, label: 'C' }));
    expect(table({ columns: wide, rows: Array.from({ length: 126 }, () => ({})) }).kind).toBe('unreadable');  // 1008 cells
  });
});

describe('a button form’s answer', () => {
  const { form } = readRenderSpec(example());
  it('sends intent button with the pressed action and the bound values', () => {
    expect(submission(form, { note: 'later today' }, 'hold')).toEqual({
      ready: true, answers: { intent: 'button', state: { action: 'hold', note: 'later today' } } });
  });
  it('never counts the action as a missing value before a press', () => {
    expect(submission(form, {}).ready).toBe(true);
  });
});

describe('the answered line', () => {
  it('names the chosen button of a button form, and says Answered for any other', () => {
    const { form } = readRenderSpec(example());
    expect(answeredLine(form, { state: { action: 'hold', note: 'x' } })).toBe('Answered · Hold');
    expect(answeredLine(form, null)).toBe('');
    expect(answeredLine({ buttons: [] }, { state: { choice: 'send' } })).toBe('Answered');
  });
});

describe('decimals', () => {
  it('draws an integer divided by 10^decimals with exactly that many digits, in the locale', () => {
    expect(formatScaled(125, 2, 'nb-NO')).toBe('1,25');
    expect(formatScaled(120, 1, 'nb-NO')).toBe('12,0');
    expect(formatScaled(125, 1, 'en-US')).toBe('12.5');
  });
  it('never groups thousands', () => {
    expect(formatScaled(12500, 0, 'nb-NO')).toBe('12500');
    expect(formatScaled(1234567, 0, 'en-US')).toBe('1234567');
    expect(formatScaled(1234567, 2, 'nb-NO')).toBe('12345,67');
  });
});
