// Draws a render_spec form (catalog clarp.form v1 and v2, issues 428 and 673) as plain DOM and collects its
// answers. It knows nothing of the network: RenderSpecCard.svelte gives it a reading and
// receives the answers the form path takes.
//
// A component type is one function in DRAW: (element, ctx) -> Node. A control registers a
// sync() in ctx so a refresh brings it in line with the values without rebuilding it, which
// keeps focus and the caret where the user left them.
//
// Enter in a text field never presses a button: there is no <form> element, and a single-line
// field swallows Enter; a multiline field keeps its newline. A button is pressed only by a
// click or a tap (or the keyboard on the button itself).

import { ACTION_KEY, footnote, formatScaled, held, submission, words } from '@core/render-spec.js';
import { drawChart } from './render-spec-chart.js';
import { t } from '../stores/i18n.svelte.js';
import { formatDateTime } from './i18n/format.js';

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
    label.append(el('span', 'rs-required', t('form.required')));
  }
  return label;
}

// The answered card: a control's value as plain text at full contrast, never a dimmed input.
// The user's answer row is not in the chat, so this card is where the answer is read.
function answer(label, value, ctx, key, cls = '') {
  const group = el('div', 'rs-field rs-answer ' + cls);
  const shown = el('div', 'rs-value');
  shown.dataset.rsKey = key;
  if (value === '' || value == null) { shown.textContent = t('form.no_answer'); shown.classList.add('rs-empty'); }
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
      ctx.syncs.push(() => { node.textContent = words(ctx.values[key], t); });
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
      return answer(label, typeof v === 'boolean' ? t(v ? 'form.yes' : 'form.no') : '', ctx, key, 'rs-toggle-answer');
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
      group.append(el('div', 'rs-value rs-empty', t('form.no_answer')));
    }
    return group;
  },

  // ---- version 2 ----
  chart(element) {
    return drawChart(element.props);
  },
  table(element) {
    const { title, columns, rows } = element.props;
    const wrap = el('div', 'rs-table-wrap');
    const table = el('table', 'rs-table');
    if (title) table.append(el('caption', 'rs-table-title', title));
    const head = el('tr');
    for (const column of columns) {
      const th = el('th', column.align === 'end' ? 'rs-end' : '', column.label);
      th.scope = 'col';
      head.append(th);
    }
    const thead = el('thead');
    thead.append(head);
    table.append(thead);
    const body = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      for (const column of columns) {
        const value = row[column.key];
        const td = el('td', column.align === 'end' ? 'rs-end' : '');
        if (typeof value === 'boolean') {
          td.textContent = value ? '✓' : '–';
          td.setAttribute('aria-label', t(value ? 'form.yes' : 'form.no'));
          td.classList.add('rs-bool');
        } else if (typeof value === 'number') {
          td.textContent = formatScaled(value, column.decimals);
          td.classList.add('rs-num');
        } else if (typeof value === 'string') {
          td.textContent = value;
        }
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    wrap.append(table);
    return wrap;
  },
  button(element, ctx) {
    const { label, style, action } = element.props;
    const button = el('button', `rs-action rs-${style}`);
    button.type = 'button';
    button.dataset.rsAction = action;
    button.append(el('span', 'rs-action-mark'), el('span', 'rs-action-label', label));
    button.addEventListener('click', () => ctx.press(element.props, button));
    ctx.actions.push({ props: element.props, button });
    return button;
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
    actions: [],
  };
  const buttonForm = form.buttons && form.buttons.length > 0;
  const chosen = accepted ? accepted.state[ACTION_KEY] : null;
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
  // A button form has no footer submit (v2): its buttons are elements of the form.
  const submit = el('button', 'rs-submit start-btn primary', form.submitLabel || '');
  submit.type = 'button';
  submit.addEventListener('click', () => send(null));
  // A button with `confirm` asks it first, here: OK submits, Cancel or Escape does not.
  const confirmBar = el('div', 'rs-confirm');
  confirmBar.setAttribute('role', 'alertdialog');
  confirmBar.hidden = true;
  const confirmText = el('div', 'rs-confirm-text');
  const confirmCancel = el('button', 'rs-action rs-secondary', t('form.cancel'));
  confirmCancel.type = 'button';
  const confirmOk = el('button', 'rs-action', t('form.ok'));
  confirmOk.type = 'button';
  const confirmRow = el('div', 'rs-confirm-row');
  confirmRow.append(confirmCancel, confirmOk);
  confirmBar.append(confirmText, confirmRow);
  let asking = null;
  const closeConfirm = () => {
    if (!asking) return;
    const from = asking.button;
    asking = null;
    confirmBar.hidden = true;
    refresh();
    from?.focus?.();
  };
  confirmCancel.addEventListener('click', closeConfirm);
  confirmOk.addEventListener('click', () => {
    const action = asking && asking.props.action;
    asking = null;
    confirmBar.hidden = true;
    if (action) send(action);
  });
  ctx.press = (props, button) => {
    if (ctx.readOnly || sending || closed || asking) return;
    if (!submission(form, ctx.values, props.action).ready) return;
    if (props.confirm) {
      asking = { props, button };
      confirmText.textContent = props.confirm;
      confirmOk.className = `rs-action rs-${props.style === 'secondary' ? 'primary' : props.style}`;
      confirmBar.hidden = false;
      refresh();
      confirmOk.focus();
      return;
    }
    send(props.action);
  };
  footer.append(confirmBar, line);
  if (!buttonForm) footer.append(submit);
  host.replaceChildren(body, footer);
  host.classList.toggle('rs-answered', ctx.readOnly);
  // Keys typed in the form are the form's: the app's shortcuts (Enter opens the composer,
  // letters split panes outside insert mode) never see them. Escape leaves the field.
  host.onkeydown = e => {
    if (e.key === 'Escape') {
      if (asking) closeConfirm();
      else document.activeElement?.blur?.();
    }
    e.stopPropagation();
  };

  function refresh() {
    for (const sync of ctx.syncs) sync();
    const result = submission(form, ctx.values);
    const short = {};
    if (!result.ready) for (const g of result.gaps) if (g.gap === 'too_short') short[g.key] = g.limit;
    for (const [key, node] of Object.entries(ctx.fieldNotes)) {
      node.hidden = !(key in short) || ctx.readOnly;
      if (key in short) node.textContent = t('form.at_least', { count: short[key] });
    }
    submit.hidden = ctx.readOnly;
    submit.disabled = !result.ready || sending || ctx.readOnly || closed;
    submit.classList.toggle('sending', sending);
    submit.textContent = form.submitLabel || '';
    // Every button is off until the values are complete, while one is being sent or asked,
    // and for good after the one answer; the answered card marks the button that was chosen.
    for (const { props, button } of ctx.actions) {
      button.disabled = ctx.readOnly || !result.ready || sending || closed || !!asking;
      const was = chosen === props.action;
      button.classList.toggle('chosen', ctx.readOnly && was);
      button.classList.toggle('not-chosen', ctx.readOnly && !was);
      if (ctx.readOnly) button.setAttribute('aria-pressed', was ? 'true' : 'false');
    }
    line.textContent = footnote({ form, values: ctx.values, accepted, note, closed, captions: ctx.captions,
      tr: t, formatWhen: ms => formatDateTime(ms) });
    line.hidden = !line.textContent;
  }

  function send(action) {
    if (ctx.readOnly || sending || closed) return;
    if (buttonForm && !action) return;
    const result = submission(form, ctx.values, action);
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
