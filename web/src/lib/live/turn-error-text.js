// The words of a failed turn's card (docs/turn-errors.md "Catalog keys", docs/i18n.md): the
// Host sends a code (`error.key`) and its parameters, and the card says the sentence from the
// app's catalog (t) in the chosen language. The Host's own `text` is only the fallback, for a
// key the catalog lacks or a placeholder left empty. `{time}` is written by the i18n formatter:
// the time alone today, with the weekday within a week, else with the date.

import en from '../i18n/en.json';
import { i18n, t } from '../../stores/i18n.svelte.js';
import { formatTime } from '../i18n/format.js';

/** `{time}`: `ms` in the language's own way, against `now`. */
export function errorTime(ms, now = Date.now(), locale = i18n.locale) {
  const at = new Date(ms);
  if (!Number.isFinite(at.getTime())) return '';
  const midnight = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(at) - midnight(new Date(now))) / 86400000);
  const sameYear = at.getFullYear() === new Date(now).getFullYear();
  const opts = days === 0 ? { hour: 'numeric', minute: '2-digit' }
    : Math.abs(days) < 7 ? { weekday: 'short', hour: 'numeric', minute: '2-digit' }
    : { day: 'numeric', month: 'short', year: sameYear ? undefined : 'numeric', hour: 'numeric', minute: '2-digit' };
  return formatTime(at, opts, locale);
}

/** Whether the shown language's own catalog has `key`: t() falls back to English, which for a
 * sentence the Host also sends is worse than the Host's own words in that language. */
function ownEntry(key, locale) {
  return locale === 'en' || t(key) !== en[key];
}

/** The catalog's text for `key` filled from `params` (`{time}` from `time_ms`); null when the
 * language's own catalog lacks the key or a placeholder its English uses is empty. */
export function sayError(key, params = {}, now = Date.now(), locale = i18n.locale) {
  const english = en[key];
  if (typeof english !== 'string' || !ownEntry(key, locale)) return null;
  const values = {};
  for (const [, name] of english.matchAll(/\{(\w+)\}/g)) {
    const value = name === 'time'
      ? (params.time_ms ? errorTime(params.time_ms, now, locale) : '')
      : params[name] == null ? '' : String(params[name]);
    if (!value.trim()) return null;
    values[name] = value;
  }
  return t(key, values);
}

/** The Host's own words: Norwegian when the app shows Norwegian, else English. */
export function hostWords(words, locale = i18n.locale) {
  if (!words) return '';
  if (typeof words === 'string') return words;
  return String((locale === 'nb' ? words.nb || words.en : words.en || words.nb) || '');
}

/** The card's sentence: the catalog's by key, else the Host's text. */
export function errorSentence(error, now = Date.now(), locale = i18n.locale) {
  return (error.key && sayError(error.key, error.params || {}, now, locale)) || hostWords(error.text, locale);
}

/** A button's words (`error.action.<id>`, `{time}` its `at_ms`), else the Host's label. */
export function actionLabel(action, now = Date.now(), locale = i18n.locale) {
  return sayError(`error.action.${action.id}`, action.at_ms ? { time_ms: action.at_ms } : {}, now, locale) || hostWords(action.label, locale);
}
