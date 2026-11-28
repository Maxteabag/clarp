// Native forms (render_spec, catalog clarp.form v1 and v2; issues 428, 673): what this client makes of a
// payload, and the answers it sends. No DOM and no network here: the drawing is
// web/src/lib/render-spec-draw.js, the loading and sending RenderSpecCard.svelte.
//
// The Host's contract/catalogs/clarp.form.v1.json is the authority; this reader checks every
// rule of it again, independently of the Host that published the form, the same rules the iOS
// app's reader checks. A form this build cannot draw is 'newer' (a catalog, version, component
// or property it does not know) or 'unreadable' (broken against the catalog it knows).

import { english } from './i18n-en.js';

// The client contract this web build declares in X-Clarp-Client (`web/<build> contract=<n>`).
// The web app's own series: 1 is the first build that draws and answers render_spec (v1), 2 the
// first that also draws v2 (chart, table, button). The Host admits it by these numbers
// (server_identity.RENDER_SPEC_MIN_CONTRACT["web"], RENDER_SPEC_V2_MIN_CONTRACT["web"]).
export const WEB_CLIENT_CONTRACT = 2;
export const WEB_CLIENT_NAME = 'web';

/** The X-Clarp-Client value: strict `<name>/<build> contract=<n>`, nothing after it. */
export function clientHeader(build) {
  const safe = String(build || '').replace(/[^A-Za-z0-9._-]/g, '').slice(0, 80) || '0';
  return `${WEB_CLIENT_NAME}/${safe} contract=${WEB_CLIENT_CONTRACT}`;
}

export const CATALOG = Object.freeze({
  name: 'clarp.form',
  versions: [1, 2],
  maxElements: 200,
  maxDepth: 12,
  maxSpecBytes: 32768,
  maxStateBytes: 4096,
  maxAnswerBytes: 16384,
});

// Component name to its allowed property names (the catalog's `components`). A type is one
// entry here and one reader in READERS below; its drawing is one function in render-spec-draw.js.
export const COMPONENTS = Object.freeze({
  stack: ['axis'],
  card: ['title'],
  heading: ['text', 'level'],
  text: ['text'],
  text_field: ['label', 'placeholder', 'multiline', 'value'],
  toggle: ['label', 'value'],
  choice: ['label', 'options', 'value'],
  divider: [],
});
// Version 2 is version 1 plus three components (contract/catalogs/clarp.form.v2.json).
export const COMPONENTS_V2 = Object.freeze({
  ...COMPONENTS,
  chart: ['kind', 'title', 'x_label', 'y_label', 'unit', 'decimals', 'series'],
  table: ['title', 'columns', 'rows'],
  button: ['label', 'action', 'style', 'confirm'],
});
const CONTAINERS = new Set(['stack', 'card']);
// A button form's answer: every button submits with this intent and names itself under this key.
export const BUTTON_INTENT = 'button';
export const ACTION_KEY = 'action';

const IDENT = /^[a-z][a-z0-9_]{0,39}$/;
export function isIdentifier(text) { return typeof text === 'string' && IDENT.test(text); }

/** Length as the Host counts it: Unicode code points. */
export function length(text) { return [...String(text)].length; }

const isObject = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const isInt = v => typeof v === 'number' && Number.isSafeInteger(v);

// ---- canonical JSON (RFC 8785 for the contract's subset: no fractions) ----------------------

function canonicalText(value) {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalText).join(',') + ']';
  // Keys in UTF-16 code unit order, which is what the default sort compares.
  const keys = Object.keys(value).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalText(value[k])).join(',') + '}';
}

/** The canonical UTF-8 bytes the Host hashes and measures. */
export function canonical(value) {
  return new TextEncoder().encode(canonicalText(value));
}

// ---- reading ---------------------------------------------------------------------------------

class Stop extends Error {
  constructor(kind, pointer = '') { super(kind); this.kind = kind; this.pointer = pointer; }
}
const broken = pointer => new Stop('unreadable', pointer);
const newer = () => new Stop('newer');

function text(value, pointer, min, max) {
  if (typeof value !== 'string') throw broken(pointer);
  const n = length(value);
  if (n < min || n > max) throw broken(pointer);
  return value;
}

function closed(object, allowed, pointer) {
  const extra = Object.keys(object).sort().find(k => !allowed.includes(k));
  if (extra !== undefined) throw broken(pointer + '/' + (isIdentifier(extra) ? extra : ''));
}

function schemaFields(payload) {
  const base = '/answer_schema';
  const schema = payload.answer_schema;
  if (!isObject(schema)) throw broken(base);
  closed(schema, ['type', 'properties', 'required', 'additionalProperties'], base);
  if (schema.type !== 'object') throw broken(base + '/type');
  if ('additionalProperties' in schema && schema.additionalProperties !== false) {
    throw broken(base + '/additionalProperties');
  }
  const properties = schema.properties ?? {};
  if (!isObject(properties) || !Object.keys(properties).every(isIdentifier)) throw broken(base + '/properties');
  const fields = {};
  for (const [key, property] of Object.entries(properties)) {
    const pointer = `${base}/properties/${key}`;
    if (!isObject(property)) throw broken(pointer);
    closed(property, ['type', 'enum', 'minLength', 'maxLength'], pointer);
    const type = property.type;
    if ('enum' in property) {
      const options = property.enum;
      if ((type !== undefined && type !== 'string') || 'minLength' in property || 'maxLength' in property
          || !Array.isArray(options) || !options.length || !options.every(v => typeof v === 'string')
          || new Set(options).size !== options.length) {
        throw broken(pointer + '/enum');
      }
      fields[key] = { kind: 'choice', options: [...options] };
      continue;
    }
    if (type === 'string') {
      const minimum = 'minLength' in property ? (isInt(property.minLength) ? property.minLength : -1) : 0;
      const maximum = 'maxLength' in property ? (isInt(property.maxLength) ? property.maxLength : -1) : null;
      if (minimum < 0 || (maximum !== null && maximum < minimum)) throw broken(pointer);
      fields[key] = { kind: 'string', minLength: minimum, maxLength: maximum };
    } else if (type === 'boolean' || type === 'integer') {
      if ('minLength' in property || 'maxLength' in property) throw broken(pointer);
      fields[key] = { kind: type };
    } else {
      throw broken(pointer + '/type');
    }
  }
  const names = schema.required ?? [];
  if (!Array.isArray(names) || !names.every(n => typeof n === 'string' && fields[n])
      || new Set(names).size !== names.length) {
    throw broken(base + '/required');
  }
  return { fields, required: new Set(names) };
}

function stateKey(value, operator, pointer) {
  if (!isObject(value) || Object.keys(value).length !== 1) throw broken(pointer);
  const path = value[operator];
  if (typeof path !== 'string' || !path.startsWith('/') || !isIdentifier(path.slice(1))) throw broken(pointer);
  return path.slice(1);
}

const sameList = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

// One reader per component type: the catalog's props in, the props the drawing uses out.
// `ctx.bind(test)` takes the element's `value` binding and checks the key's field.
const READERS = {
  stack(props, base) {
    const axis = 'axis' in props ? props.axis : 'vertical';
    if (axis !== 'vertical' && axis !== 'horizontal') throw broken(base + '/props/axis');
    return { horizontal: axis === 'horizontal' };
  },
  card(props, base) {
    return { title: 'title' in props ? text(props.title, base + '/props/title', 0, 120) : null };
  },
  heading(props, base) {
    const level = 'level' in props ? props.level : 2;
    if (!isInt(level) || level < 1 || level > 3) throw broken(base + '/props/level');
    return { text: text(props.text, base + '/props/text', 0, 200), level };
  },
  text(props, base, ctx) {
    if (typeof props.text === 'string') return { text: text(props.text, base + '/props/text', 0, 4000) };
    const key = stateKey(props.text, '$state', base + '/props/text');
    if (!ctx.fields[key]) throw broken(base + '/props/text');
    return { stateKey: key };
  },
  text_field(props, base, ctx) {
    const label = text(props.label, base + '/props/label', 1, 120);
    const placeholder = 'placeholder' in props ? text(props.placeholder, base + '/props/placeholder', 0, 120) : null;
    const multiline = 'multiline' in props ? props.multiline : false;
    if (typeof multiline !== 'boolean') throw broken(base + '/props/multiline');
    const key = ctx.bind(f => f.kind === 'string');
    return { label, placeholder, multiline, key };
  },
  toggle(props, base, ctx) {
    const label = text(props.label, base + '/props/label', 1, 120);
    const key = ctx.bind(f => f.kind === 'boolean');
    ctx.toggles.push(key);
    return { label, key };
  },
  choice(props, base, ctx) {
    const label = text(props.label, base + '/props/label', 1, 120);
    const raw = props.options;
    if (!Array.isArray(raw) || raw.length < 2 || raw.length > 12) throw broken(base + '/props/options');
    const options = raw.map(item => {
      if (!isObject(item) || !sameList(Object.keys(item).sort(), ['label', 'value'])) throw broken(base + '/props/options');
      return {
        value: text(item.value, base + '/props/options', 1, 120),
        label: text(item.label, base + '/props/options', 1, 120),
      };
    });
    // The answer key's enum equals the option values, in order.
    const key = ctx.bind(f => f.kind === 'choice' && sameList(f.options, options.map(o => o.value)));
    return { label, options, key };
  },
  divider() { return {}; },

  // ---- version 2 ----
  chart(props, base) {
    const kind = props.kind;
    if (!['bar', 'line', 'pie'].includes(kind)) throw broken(base + '/props/kind');
    const opt = (key, max) => (key in props ? text(props[key], `${base}/props/${key}`, 0, max) : null);
    const title = opt('title', 120), xLabel = opt('x_label', 60), yLabel = opt('y_label', 60), unit = opt('unit', 12);
    const decimals = readDecimals(props, base + '/props/decimals');
    const sp = base + '/props/series';
    const raw = props.series;
    if (!Array.isArray(raw) || raw.length < 1 || raw.length > 6) throw broken(sp);
    const series = raw.map((one, i) => {
      if (!isObject(one)) throw broken(`${sp}/${i}`);
      closed(one, ['name', 'points'], `${sp}/${i}`);
      const name = text(one.name, `${sp}/${i}/name`, 1, 60);
      const pp = `${sp}/${i}/points`;
      if (!Array.isArray(one.points) || one.points.length < 1 || one.points.length > 200) throw broken(pp);
      const points = one.points.map((point, j) => {
        if (!isObject(point) || !('x' in point) || !('y' in point)) throw broken(`${pp}/${j}`);
        closed(point, ['x', 'y'], `${pp}/${j}`);
        const x = point.x;
        if (typeof x === 'string') text(x, `${pp}/${j}/x`, 1, 60);
        else if (!isInt(x)) throw broken(`${pp}/${j}/x`);
        if (!isInt(point.y)) throw broken(`${pp}/${j}/y`);
        return { x, y: point.y };
      });
      return { name, points };
    });
    // What the schema cannot say, in the Host's order (render_spec._check_chart).
    series.forEach((one, i) => {
      if (series.findIndex(other => other.name === one.name) < i) throw broken(`${sp}/${i}/name`);
    });
    if (kind === 'pie' && series.length !== 1) throw broken(sp);
    if (series.reduce((n, one) => n + one.points.length, 0) > 600) throw broken(sp);
    let strings = null;
    series.forEach((one, i) => {
      const pp = `${sp}/${i}/points`;
      if (kind === 'bar' && one.points.length > 60) throw broken(pp);
      if (kind === 'pie' && one.points.length > 12) throw broken(pp);
      const seen = new Set();
      let last = null;
      one.points.forEach(({ x, y }, j) => {
        if (strings === null) strings = typeof x === 'string';
        else if ((typeof x === 'string') !== strings) throw broken(`${pp}/${j}/x`);
        if (seen.has(x)) throw broken(`${pp}/${j}/x`);
        if (!strings && last !== null && x <= last) throw broken(`${pp}/${j}/x`);
        seen.add(x);
        last = x;
        if (kind === 'pie' && y < 0) throw broken(`${pp}/${j}/y`);
      });
    });
    return { kind, title, xLabel, yLabel, unit, decimals, series, numericX: strings === false };
  },
  table(props, base) {
    const title = 'title' in props ? text(props.title, base + '/props/title', 0, 120) : null;
    const cp = base + '/props/columns';
    if (!Array.isArray(props.columns) || props.columns.length < 1 || props.columns.length > 8) throw broken(cp);
    const keys = [];
    const columns = props.columns.map((column, i) => {
      if (!isObject(column)) throw broken(`${cp}/${i}`);
      closed(column, ['key', 'label', 'align', 'decimals'], `${cp}/${i}`);
      if (!isIdentifier(column.key) || keys.includes(column.key)) throw broken(`${cp}/${i}/key`);
      keys.push(column.key);
      const align = 'align' in column ? column.align : 'start';
      if (align !== 'start' && align !== 'end') throw broken(`${cp}/${i}/align`);
      return {
        key: column.key, label: text(column.label, `${cp}/${i}/label`, 1, 60), align,
        decimals: readDecimals(column, `${cp}/${i}/decimals`),
      };
    });
    const rp = base + '/props/rows';
    if (!Array.isArray(props.rows) || props.rows.length > 200) throw broken(rp);
    if (props.rows.length * keys.length > 1000) throw broken(rp);
    const rows = props.rows.map((row, i) => {
      if (!isObject(row)) throw broken(`${rp}/${i}`);
      closed(row, keys, `${rp}/${i}`);
      for (const [key, value] of Object.entries(row)) {
        const ok = value === null || typeof value === 'boolean' || isInt(value)
          || (typeof value === 'string' && length(value) <= 200);
        if (!ok) throw broken(`${rp}/${i}/${key}`);
      }
      return row;
    });
    return { title, columns, rows };
  },
  button(props, base, ctx) {
    const label = text(props.label, base + '/props/label', 1, 40);
    if (!isIdentifier(props.action) || ctx.buttons.some(b => b.action === props.action)) throw broken(base + '/props/action');
    const style = 'style' in props ? props.style : 'secondary';
    if (!['primary', 'secondary', 'destructive'].includes(style)) throw broken(base + '/props/style');
    const confirm = 'confirm' in props ? text(props.confirm, base + '/props/confirm', 1, 200) : null;
    const button = { action: props.action, label, style, confirm };
    ctx.buttons.push(button);
    return button;
  },
};

function readDecimals(props, pointer) {
  if (!('decimals' in props)) return 0;
  const d = props.decimals;
  if (!isInt(d) || d < 0 || d > 6) throw broken(pointer);
  return d;
}

/**
 * An integer drawn as the catalog's `decimals` say: divided by 10^decimals, in the device's
 * locale, with exactly that many digits after the separator (125, 2 -> "1,25" in nb), and
 * never grouped in thousands (12500 stays "12500"), as every client draws it (issue 673).
 */
export function formatScaled(value, decimals = 0, locale = undefined) {
  if (!isInt(value)) return '';
  return new Intl.NumberFormat(locale, { minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: false })
    .format(value / 10 ** decimals);
}

function readElement(id, elements, ctx, depth, underCard) {
  const base = '/spec/elements/' + id;
  const raw = elements[id];
  if (depth > CATALOG.maxDepth || ctx.visited.has(id) || !isObject(raw)) throw broken(base);
  ctx.visited.add(id);
  closed(raw, ['type', 'props', 'children'], base);
  const type = raw.type;
  if (typeof type !== 'string') throw broken(base + '/type');
  const allowed = ctx.components[type];
  if (!Object.hasOwn(ctx.components, type)) throw newer();
  const props = raw.props ?? {};
  if (!isObject(props)) throw broken(base + '/props');
  if (!Object.keys(props).every(k => allowed.includes(k))) throw newer();
  const childIds = raw.children ?? [];
  if (!Array.isArray(childIds) || !childIds.every(c => typeof c === 'string')
      || (childIds.length && !CONTAINERS.has(type))) {
    throw broken(base + '/children');
  }
  if (type === 'card' && underCard) throw broken(base);
  const bind = test => {
    const pointer = base + '/props/value';
    const key = stateKey(props.value, '$bindState', pointer);
    const field = ctx.fields[key];
    if (!field || !test(field) || ctx.bound.has(key)) throw broken(pointer);
    ctx.bound.add(key);
    return key;
  };
  const read = READERS[type](props, base, { ...ctx, bind });
  const children = childIds.map(c => readElement(c, elements, ctx, depth + 1, underCard || type === 'card'));
  return { id, type, props: read, children };
}

/**
 * What this build makes of a render_spec: `{kind: 'form', form}`, `{kind: 'newer'}` or
 * `{kind: 'unreadable', pointer}`. `payload` is the flat artifact of the single read
 * (GET /artifacts/<id>) or a bare payload.
 */
export function readRenderSpec(payload) {
  try {
    return { kind: 'form', form: readForm(payload) };
  } catch (stop) {
    if (stop instanceof Stop) return stop.kind === 'newer' ? { kind: 'newer' } : { kind: 'unreadable', pointer: stop.pointer };
    return { kind: 'unreadable', pointer: '' };
  }
}

function readForm(payload) {
  if (!isObject(payload) || payload.catalog !== CATALOG.name || !CATALOG.versions.includes(payload.catalog_version)) throw newer();
  const catalogVersion = payload.catalog_version;
  const version = text(payload.version, '/version', 1, 80);
  const spec = payload.spec;
  if (!isObject(spec)) throw broken('/spec');
  closed(spec, ['root', 'elements', 'state'], '/spec');
  if (canonical(spec).length > CATALOG.maxSpecBytes) throw broken('/spec');
  const { fields, required } = schemaFields(payload);
  const elements = spec.elements;
  if (!isObject(elements)) throw broken('/spec/elements');
  const count = Object.keys(elements).length;
  if (count < 1 || count > CATALOG.maxElements || !Object.keys(elements).every(isIdentifier)) throw broken('/spec/elements');
  if (typeof spec.root !== 'string' || !Object.hasOwn(elements, spec.root)) throw broken('/spec/root');
  const components = catalogVersion === 2 ? COMPONENTS_V2 : COMPONENTS;
  const ctx = { fields, components, visited: new Set(), bound: new Set(), toggles: [], buttons: [] };
  const root = readElement(spec.root, elements, ctx, 1, false);
  // One rooted tree: nothing left over, nothing shared.
  if (ctx.visited.size !== count) throw broken('/spec/elements');
  const state = spec.state ?? {};
  if (!isObject(state) || canonical(state).length > CATALOG.maxStateBytes) throw broken('/spec/state');
  const initial = {};
  for (const [key, value] of Object.entries(state)) {
    const pointer = '/spec/state/' + (isIdentifier(key) ? key : '');
    if (!ok(fields[key], value)) throw broken(pointer);
    initial[key] = value;
  }
  for (const key of ctx.toggles) if (!(key in initial)) initial[key] = false;
  const buttons = ctx.buttons;
  let submitIntent = BUTTON_INTENT, submitLabel = null;
  if (buttons.length) {
    // A button form (v2): no footer submit; the schema's action is a required string whose enum
    // is exactly the button actions, and no element binds it.
    const rule = payload.answer_schema.properties[ACTION_KEY];
    const actions = buttons.map(b => b.action);
    const at = '/answer_schema/properties/' + ACTION_KEY;
    if (!isObject(rule) || !sameList(Object.keys(rule).sort(), ['enum', 'type']) || rule.type !== 'string'
        || !fields[ACTION_KEY] || fields[ACTION_KEY].options.length !== actions.length
        || !actions.every(a => fields[ACTION_KEY].options.includes(a)) || !required.has(ACTION_KEY)) {
      throw broken(at);
    }
    if (ctx.bound.has(ACTION_KEY)) throw broken(at);
    if ('submit' in payload) throw broken('/submit');
  } else {
    const submit = payload.submit;
    if (!isObject(submit)) throw broken('/submit');
    closed(submit, ['intent', 'label'], '/submit');
    if (!isIdentifier(submit.intent)) throw broken('/submit/intent');
    submitIntent = submit.intent;
    submitLabel = text(submit.label, '/submit/label', 1, 40);
  }
  return {
    version, catalogVersion, root, submitIntent, submitLabel, buttons, fields, required, initial,
    boundKeys: ctx.bound, spec,
  };
}

/** Whether `value` is one the field takes (a declared initial value, a carried draft). */
function ok(field, value) {
  if (!field) return false;
  switch (field.kind) {
    case 'string': {
      if (typeof value !== 'string') return false;
      const n = length(value);
      return n >= field.minLength && (field.maxLength === null || n <= field.maxLength);
    }
    case 'choice': return typeof value === 'string' && field.options.includes(value);
    case 'boolean': return typeof value === 'boolean';
    case 'integer': return isInt(value);
    default: return false;
  }
}

/**
 * The answers exactly as the form path takes them: `{intent, state}`, with only keys that have a
 * value. An empty text field and an unselected choice are left out; a toggle is always there.
 * `{ready: true, answers}` or `{ready: false, gaps}`; a gap is `{gap: 'missing'|'too_short'|
 * 'too_long'|'too_large', key?, limit?}`.
 */
export function submission(form, values, action = null) {
  const state = {};
  const gaps = [];
  const buttons = form.buttons && form.buttons.length > 0;
  for (const key of Object.keys(form.fields).sort()) {
    // A button form's action is the button pressed, never a value typed.
    if (buttons && key === ACTION_KEY) continue;
    const field = form.fields[key];
    const value = values[key];
    const needed = form.required.has(key);
    if (field.kind === 'string' && typeof value === 'string') {
      const n = length(value);
      if (n === 0) { if (needed) gaps.push({ gap: 'missing', key }); }
      else if (n < field.minLength) gaps.push({ gap: 'too_short', key, limit: field.minLength });
      else if (field.maxLength !== null && n > field.maxLength) gaps.push({ gap: 'too_long', key, limit: field.maxLength });
      else state[key] = value;
    } else if (field.kind === 'choice' && typeof value === 'string' && field.options.includes(value)) {
      state[key] = value;
    } else if (field.kind === 'boolean' && typeof value === 'boolean') {
      state[key] = value;
    } else if (field.kind === 'integer' && isInt(value)) {
      state[key] = value;
    } else if (needed) {
      gaps.push({ gap: 'missing', key });
    }
  }
  // Sent with the pressed button's action; before a press, measured with the longest one.
  if (buttons) state[ACTION_KEY] = action || form.buttons.map(b => b.action).sort((a, b) => b.length - a.length)[0];
  const answers = { intent: form.submitIntent, state };
  if (canonical(answers).length > CATALOG.maxAnswerBytes) gaps.push({ gap: 'too_large' });
  return gaps.length ? { ready: false, gaps } : { ready: true, answers };
}

/** Typed values survive a reload when their key still exists with a value the new field takes. */
export function carrying(form, old) {
  const values = { ...form.initial };
  for (const [key, value] of Object.entries(old || {})) {
    if (!form.boundKeys.has(key)) continue;
    const field = form.fields[key];
    if (field && field.kind !== 'integer' && (field.kind === 'string' ? typeof value === 'string' : ok(field, value))) {
      values[key] = value;
    }
  }
  return values;
}

/** Keeps a text within the key's maxLength, counted as the Host counts it. */
export function held(form, key, value) {
  const field = form.fields[key];
  if (!field || field.kind !== 'string' || field.maxLength === null || length(value) <= field.maxLength) return value;
  return [...value].slice(0, field.maxLength).join('');
}

/** The one accepted submission as the single read returns it, or null. */
export function acceptedSubmission(value) {
  if (!isObject(value) || typeof value.submission_id !== 'string' || typeof value.version !== 'string') return null;
  const answers = value.answers;
  if (!isObject(answers) || typeof answers.intent !== 'string' || !isObject(answers.state)) return null;
  if (!isInt(value.accepted_at) || typeof value.delivery_status !== 'string') return null;
  const state = {};
  for (const [key, v] of Object.entries(answers.state)) {
    if (typeof v !== 'string' && typeof v !== 'boolean' && !isInt(v)) return null;
    state[key] = v;
  }
  return {
    submissionId: value.submission_id, version: value.version, intent: answers.intent, state,
    acceptedAt: value.accepted_at, deliveryStatus: value.delivery_status,
  };
}

/**
 * The answered card's line (issue 673): 'Answered · <button label>' for a button form, naming
 * the button that was chosen; 'Answered' for any other answered form; '' while it is open.
 */
export function answeredLine(form, accepted, tr = english) {
  if (!accepted) return '';
  const chosen = form && form.buttons && form.buttons.find(b => b.action === accepted.state[ACTION_KEY]);
  return chosen ? tr('form.answered_with', { label: chosen.label }) : tr('form.answered');
}

/** The words a state-bound text shows for a value. */
export function words(value, tr = english) {
  if (typeof value === 'boolean') return tr(value ? 'form.yes' : 'form.no');
  if (value === undefined || value === null) return '';
  return String(value);
}

/**
 * The line above Submit: the answered note, a note from the owner, or why Submit is off.
 * `captions` maps a key to its control's label.
 */
export function footnote({ form, values, accepted = null, note = '', closed = false, captions = {},
  tr = english, formatWhen = ms => new Date(ms).toLocaleString('en', { dateStyle: 'medium', timeStyle: 'short' }) }) {
  if (accepted) {
    const when = formatWhen(new Date(accepted.acceptedAt).getTime());
    return tr(accepted.deliveryStatus === 'delivered' ? 'form.answered_at_delivered' : 'form.answered_at_pending', { when });
  }
  if (note) return note;
  if (closed) return '';
  const result = submission(form, values);
  if (result.ready) return '';
  if (result.gaps.some(g => g.gap === 'too_large')) return tr('form.too_long');
  const missing = result.gaps.filter(g => g.gap === 'missing').length;
  if (missing) return tr('form.missing', { count: missing });
  const short = result.gaps.filter(g => g.gap === 'too_short').sort((a, b) => (a.key < b.key ? -1 : 1))[0];
  if (short) {
    return tr('form.too_short', { field: captions[short.key] || tr('form.a_text'), count: short.limit });
  }
  return '';
}

/**
 * What a refusal of the form path means to this client (the code, never the Host's words):
 * reopen | answered | closed | too_old | answer | host_fault | reused | other.
 */
export function refusalMeaning(code) {
  switch (code) {
    case 'version_mismatch': return 'reopen';
    case 'already_answered': return 'answered';
    case 'not_open': return 'closed';
    case 'client_contract': return 'too_old';
    case 'state_value': case 'missing_key': case 'answer_size': return 'answer';
    case 'catalog_unavailable': return 'host_fault';
    case 'reused_id': return 'reused';
    default: return 'other';
  }
}

/** A submission id the Host takes (`[A-Za-z0-9-]{16,80}`), fresh per attempt. */
export function newSubmissionId() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Whether the spec read is the one the Host's witness names (`content_identity`:
 * version, canonical byte count, `c1:` + SHA-256 hex). `digest` maps bytes to lowercase hex;
 * null when hashing is unavailable (a page outside a secure context), then only the version
 * and byte count are compared.
 */
export async function matchesIdentity(form, identity, digest) {
  if (!isObject(identity)) return false;
  const raw = canonical(form.spec);
  if (identity.version !== form.version || identity.bytes !== raw.length) return false;
  if (!digest) return true;
  return identity.sha256 === 'c1:' + await digest(raw);
}

// ---- placing cards in the transcript --------------------------------------------------------

function entryTime(entry, liveStartMs) {
  // A later part of a live turn split at a steer (withLiveSegments) starts at that steer.
  if (entry.type === 'live' && Number.isFinite(entry.from)) return entry.from;
  if (entry.type === 'live') return Number.isFinite(liveStartMs) ? liveStartMs : Infinity;
  const item = entry.item || {};
  const raw = entry.type === 'turn' ? item.timestamp : item.ts;
  if (raw == null) return null;
  const ms = typeof raw === 'number' ? raw : Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * The transcript's entries with one keyed `artifact` entry per render_spec row, each before
 * the first entry written after it was published. A row older than the first loaded entry is
 * left for an older page unless the chat has no older page (`complete`).
 */
export function withArtifacts(entries, rows, { complete = false, liveStartMs = null } = {}) {
  if (!rows || !rows.length) return entries;
  const cards = rows
    .filter(r => r && r.artifact_id && Number.isFinite(r.created_at))
    .slice()
    .sort((a, b) => a.created_at - b.created_at || (a.artifact_id < b.artifact_id ? -1 : 1));
  if (!cards.length) return entries;
  const out = [];
  let next = 0;
  let first = true;
  let carried = null;
  for (const entry of entries) {
    const at = entryTime(entry, liveStartMs);
    if (at !== null) carried = at;
    if (carried !== null) {
      while (next < cards.length && cards[next].created_at < carried) {
        if (!first || complete) out.push(card(cards[next]));
        next += 1;
      }
      first = false;
    }
    out.push(entry);
  }
  while (next < cards.length) {
    if (!first || complete || !entries.length) out.push(card(cards[next]));
    next += 1;
  }
  return out;
}

function card(row) {
  return { type: 'artifact', key: 'r:' + row.artifact_id, item: row };
}
