// A failed turn's sentence in the app's language (docs/turn-errors.md, feature
// turn_error_explained). The Host sends `error.key` and `params`; the app words it from its own
// catalog, and falls back to the Host's `text` (Norwegian for a Norwegian app, else English) when
// the language shown has no text of its own for the key or a placeholder it uses is empty.

import en from './i18n/en.json';
import { i18n, t, hasOwn } from '../stores/i18n.svelte.js';
import { formatDateTime, formatTime } from './i18n/format.js';

/** `{time}` of an error: the time alone today, with the weekday within a week, else with the date. */
export function errorTime(ms, now = Date.now()) {
  const d = new Date(ms);
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  if (d >= today && d - today < 86400000) return formatTime(d);
  if (Math.abs(d - today) < 6 * 86400000) {
    return formatDateTime(d, { weekday: 'long', hour: 'numeric', minute: '2-digit' });
  }
  return formatDateTime(d);
}

function placeholders(key) {
  const v = en[key];
  const s = v && typeof v === 'object' ? Object.values(v).join(' ') : String(v ?? '');
  return [...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
}

function paramsOf(e, atMs) {
  const p = { ...(e.params && typeof e.params === 'object' ? e.params : {}) };
  const ms = atMs ?? p.time_ms;
  if (ms != null && Number.isFinite(Number(ms))) p.time = errorTime(Number(ms));
  return p;
}

function catalog(key, params) {
  if (!key || !hasOwn(key)) return null;
  if (placeholders(key).some(n => params[n] == null || params[n] === '')) return null;
  return t(key, params);
}

function hostText(text) {
  if (!text || typeof text !== 'object') return typeof text === 'string' ? text : '';
  return (i18n.locale === 'nb' && text.nb) || text.en || '';
}

/** The sentence of a turn error object; `fallback` (the Host's older message) when there is none. */
export function turnErrorText(e, fallback = '') {
  if (!e || typeof e !== 'object') return fallback;
  const key = typeof e.key === 'string' && e.key.startsWith('error.') ? e.key
    : (typeof e.code === 'string' && e.code ? `error.${e.code}` : '');
  return catalog(key, paramsOf(e)) || hostText(e.text) || e.message || fallback;
}

/** A turn error action's button words (`error.action.<id>`), its `label` as the fallback. */
export function errorActionLabel(action) {
  if (!action || typeof action !== 'object') return '';
  return catalog(`error.action.${action.id}`, paramsOf({}, action.at_ms)) || hostText(action.label);
}
