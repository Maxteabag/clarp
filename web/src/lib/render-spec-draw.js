// Draws a render_spec form (catalog clarp.form v1, issue 428) as plain DOM and collects its
// answers. It knows nothing of the network: RenderSpecCard.svelte gives it a reading and
// receives the answers the form path takes.
//
// A component type is one function in DRAW: (element, ctx) -> Node. A control registers a
// sync() in ctx so a refresh brings it in line with the values without rebuilding it, which
// keeps focus and the caret where the user left them.
//
// Enter in a text field never presses a button: there is no <form> element, and a single-line
// field swallows Enter; a multiline field keeps its newline.

import { footnote, held, submission, words } from '@core/render-spec.js';

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function caption(text, key, ctx) {
  ctx.captions[key] = text;
  const label = el('div', 'rs-caption', text);
  if (ctx.form.required.has(key) && !ctx.readOnly) {
    label.append(el('span', 'rs-required', 'Required'));
  }
  return label;
}

// The answered card: a control's value as plain text at full contrast, never a dimmed input.
// The user's answer row is not in the chat, so this card is where the answer is read.
function answer(label, value, ctx, key, cls = '') {
  const group = el('div', 'rs-field rs-answer ' + cls);
  const shown = el('div', 'rs-value');
  shown.dataset.rsKey = key;
  if (value === '' || value == null) { shown.textContent = 'No answer'; shown.classList.add('rs-empty'); }
  else shown.textContent = value;
  group.append(caption(label, key, ctx), shown);
  return group;
}

const DRAW = {
  stack(element, ctx) {
    const node = el('div', element.props.horizontal ? 'rs-stack rs-horizontal' : 'rs-stack');
    for (const child of element.children) node.append(ctx.draw(child));
    return node;
  },
  card(element, ctx) {
    const node = el('section', 'rs-card');
    if (element.props.title) node.append(el('h3', 'rs-card-title', element.props.title));
    for (const child of element.children) node.append(ctx.draw(child));
    return node;
  },
  heading(element) {
    const { level, text } = element.props;
    return el('h' + (level + 1), 'rs-heading rs-heading-' + level, text);
  },
  text(element, ctx) {
    const node = el('p', 'rs-text');
    if (element.props.stateKey) {
      const key = element.props.stateKey;
      ctx.syncs.push(() => { node.textContent = words(ctx.values[key]); });
    } else {
      node.textContent = element.props.text;
    }
    return node;
  },
  divider() {
    const node = el('hr', 'rs-divider');
    node.setAttribute('aria-hidden', 'true');
    return node;
  },
  text_field(element, ctx) {
    const { label, placeholder, multiline, key } = element.props;
    if (ctx.readOnly) return answer(label, ctx.values[key], ctx, key, multiline ? 'rs-multiline' : '');
    const group = el('label', 'rs-field');
    const input = multiline ? el('textarea', 'rs-input rs-multiline') : el('input', 'rs-input');
    if (!multiline) input.type = 'text';
    if (multiline) input.rows = 3;
    if (placeholder) input.placeholder = placeholder;
    input.value = typeof ctx.values[key] === 'string' ? ctx.values[key] : '';
    input.dataset.rsKey = key;
    input.addEventListener('input', () => {
      const kept = held(ctx.form, key, input.value);
      if (kept !== input.value) input.value = kept;
      ctx.set(key, kept);
    });
    if (!multiline) {
      input.addEventListener('keydown', e => { if (e.key === 'Enter') e.preventDefault(); });
    }
    const note = el('div', 'rs-field-note');
    note.hidden = true;
    ctx.fieldNotes[key] = note;
    group.append(caption(label, key, ctx), input, note);
    return group;
  },
  toggle(element, ctx) {
    const { label, key } = element.props;
    if (ctx.readOnly) {
      const v = ctx.values[key];
      return answer(label, typeof v === 'boolean' ? (v ? 'Yes' : 'No') : '', ctx, key, 'rs-toggle-answer');
    }
    const row = el('label', 'rs-toggle');
    const input = el('input', 'rs-switch');
    input.type = 'checkbox';
    input.setAttribute('role', 'switch');
    input.dataset.rsKey = key;
    input.addEventListener('change', () => ctx.set(key, input.checked));
    ctx.syncs.push(() => { input.checked = ctx.values[key] === true; });
    row.append(el('span', 'rs-toggle-label', label), input);
    return row;
  },
  choice(element, ctx) {
    const { label, options, key } = element.props;
    const group = el('div', ctx.readOnly ? 'rs-field rs-answer' : 'rs-field');
    const list = el('div', 'rs-options');
    list.setAttribute('role', 'radiogroup');
    list.setAttribute('aria-label', label);
    const buttons = options.map((option, index) => {
      const button = el('button', 'rs-option');
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.disabled = ctx.readOnly;
      button.dataset.rsOption = `${element.id}-${index}`;
      button.append(el('span', 'rs-option-mark'), el('span', 'rs-option-label', option.label));
      button.addEventListener('click', () => ctx.set(key, option.value));
      list.append(button);
      return [option.value, button];
    });
    ctx.syncs.push(() => {
      for (const [value, button] of buttons) {
        const chosen = ctx.values[key] === value;
        button.classList.toggle('chosen', chosen);
        button.setAttribute('aria-checked', chosen ? 'true' : 'false');
      }
    });
    group.append(caption(label, key, ctx), list);
    if (ctx.readOnly && !options.some(o => o.value === ctx.values[key])) {
      group.append(el('div', 'rs-value rs-empty', 'No answer'));
    }
    return group;
  },
};

/** Whether this build draws a component type. */
export function draws(type) { return Object.hasOwn(DRAW, type); }

/**
 * Draws `form` into `host` (replacing what was there). `accepted` (the one accepted submission)
 * makes it read-only with the answered values. Returns {setSending(flag, note), close(note),
 * values()}; `onSubmit(answers)` gets the canonical `{intent, state}` object.
 */
export function mountForm(host, { form, values, accepted = null, onSubmit, onValuesChanged }) {
  const ctx = {
    form,
    values: accepted ? { ...accepted.state } : { ...values },
    readOnly: !!accepted,
    syncs: [],
    captions: {},
    fieldNotes: {},
  };
  let sending = false;
  let closed = false;
  let note = '';

  ctx.draw = element => {
    const node = DRAW[element.type](element, ctx);
    node.dataset.rsId = element.id;
    return node;
  };
  ctx.set = (key, value) => {
    if (ctx.readOnly || ctx.values[key] === value) return;
    ctx.values[key] = value;
    if (!closed) note = '';  // an edit after a refusal must not wipe why Submit is off
    onValuesChanged?.({ ...ctx.values });
    refresh();
  };

  const body = el('div', 'rs-body');
  body.append(ctx.draw(form.root));
  const footer = el('div', 'rs-footer');
  const line = el('div', 'rs-footnote');
  line.setAttribute('aria-live', 'polite');
  const submit = el('button', 'rs-submit start-btn primary', form.submitLabel);
  submit.type = 'button';
  submit.addEventListener('click', send);
  footer.append(line, submit);
  host.replaceChildren(body, footer);
  host.classList.toggle('rs-answered', ctx.readOnly);
  // Keys typed in the form are the form's: the app's shortcuts (Enter opens the composer,
  // letters split panes outside insert mode) never see them. Escape leaves the field.
  host.onkeydown = e => {
    if (e.key === 'Escape') document.activeElement?.blur?.();
    e.stopPropagation();
  };

  function refresh() {
    for (const sync of ctx.syncs) sync();
    const result = submission(form, ctx.values);
    const short = {};
    if (!result.ready) for (const g of result.gaps) if (g.gap === 'too_short') short[g.key] = g.limit;
    for (const [key, node] of Object.entries(ctx.fieldNotes)) {
      node.hidden = !(key in short) || ctx.readOnly;
      if (key in short) node.textContent = `At least ${short[key]} characters.`;
    }
    submit.hidden = ctx.readOnly;
    submit.disabled = !result.ready || sending || ctx.readOnly || closed;
    submit.classList.toggle('sending', sending);
    submit.textContent = form.submitLabel;
    line.textContent = footnote({ form, values: ctx.values, accepted, note, closed, captions: ctx.captions });
    line.hidden = !line.textContent;
  }

  function send() {
    if (ctx.readOnly || sending || closed) return;
    const result = submission(form, ctx.values);
    if (!result.ready) return;
    onSubmit?.(result.answers);
  }

  refresh();
  return {
    setSending(flag, words = '') { sending = flag; note = words; refresh(); },
    close(words) { sending = false; closed = true; note = words; refresh(); },
    values: () => ({ ...ctx.values }),
  };
}
