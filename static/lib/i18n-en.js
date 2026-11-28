// The web app's English catalog for the shared modules in static/lib, which cannot import the
// app's language store. A module that shows words takes a translator `tr(key, params)` and
// defaults to `english`; a component passes `t` from stores/i18n.svelte.js, so the words follow
// the chosen language. The catalog itself is web/src/lib/i18n/en.json (web/docs/i18n.md).

import en from '../../web/src/lib/i18n/en.json';
import { translate } from '../../web/src/lib/i18n/core.js';

/** The English text for `key`, `{name}` filled from params, plurals by params.count. */
export function english(key, params) {
  return translate('en', en, en, key, params);
}

/** `4s`, `1m 05s`, `1h 02m`: a duration in English, for callers that pass no formatter. */
export function englishDuration(ms) {
  const pad2 = n => String(n).padStart(2, '0');
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${pad2(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${pad2(m % 60)}m`;
}
