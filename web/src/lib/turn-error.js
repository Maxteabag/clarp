// A failed turn's sentence in the app's language (docs/turn-errors.md, feature
// turn_error_explained). One implementation, in ./live/turn-error-text.js: the app words it from
// its own catalog by `error.key` and `params`, and falls back to the Host's `text` (Norwegian for a
// Norwegian app, else English) where the language shown has no text of its own for the key.

import { actionLabel, errorSentence, errorTime } from './live/turn-error-text.js';

export { errorTime };

/** The sentence of a turn error object; `fallback` (the Host's older message) when there is none. */
export function turnErrorText(e, fallback = '') {
  if (!e || typeof e !== 'object') return fallback;
  return errorSentence(e) || e.message || fallback;
}

/** A turn error action's button words (`error.action.<id>`), its `label` as the fallback. */
export function errorActionLabel(action) {
  return action && typeof action === 'object' ? actionLabel(action) : '';
}
