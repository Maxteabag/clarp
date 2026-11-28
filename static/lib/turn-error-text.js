// The words of a failed turn's card (docs/turn-errors.md "Catalog keys"):
// the Host sends a code (`error.key`) and its parameters, and the web app
// says the sentence from its own catalog in the app's language. The Host's
// own `text` is only the fallback, for a key the catalog lacks or a
// placeholder left empty. `{time}` goes through Intl.DateTimeFormat and the
// repeat count through Intl.PluralRules; nothing builds date words or plural
// endings by hand. English source under the shared keys; clarp-i18n adds the
// translations (web/src/lib/i18n) and looks them up by the same names.

export const EN = {
  'error.usage_limit': "Your {backend} account is out of usage. Try again later, or switch model.",
  'error.usage_limit_until': "Your {backend} account is out of usage until {time}. Try again then, or switch model.",
  'error.billing': "Your {backend} account is out of credits. Top it up, or switch model.",
  'error.auth': "Your {backend} sign-in has expired. Sign in again, then try again.",
  'error.account_plan': "Your {backend} plan does not include {model}. Switch model or account.",
  'error.free_model_restricted': "This free OpenCode model only runs in OpenCode's own apps. Choose another model.",
  'error.connection': "Lost the connection to {backend}, and the Host's retries did not get through. Try again.",
  'error.network_gone': "This computer had no network for 15 minutes, so the turn stopped. Try again once it is online.",
  'error.provider_busy': "{backend} is overloaded right now, and the Host's retries did not get through. Try again in a minute, or switch model.",
  'error.provider_down': "The provider of {model} is unavailable right now. Try again later, or switch model.",
  'error.invalid_request': "{backend} rejected the conversation history. Start a fresh session with the same conversation and try again.",
  'error.refused': "The model refused to answer (a safety filter blocked it). Rephrase, or switch model.",
  'error.prompt_too_long': "This conversation has grown too long for the model. Compact it and continue.",
  'error.timeout': "{backend} stopped responding. Try again.",
  'error.runner_exit': "{backend} stopped unexpectedly. Try again.",
  'error.ended_from_outside': "Something outside Clarp ended the turn. Send the message again.",
  'error.interrupted': "The turn was cut off before it finished. Send the message again.",
  'error.not_started': "The Host could not start the turn. Try again.",
  'error.folder_missing': "This agent's folder {folder} no longer exists. Recreate it, or make a new agent in another folder.",
  'error.folder_missing_no_path': "This agent's folder no longer exists. Recreate it, or make a new agent in another folder.",
  'error.provider_mismatch': "This agent ran on another model provider than the one it is bound to. Check its Codex home and provider.",
  'error.session_locked': "The {backend} session is busy in another process. Try again in a moment.",
  'error.moved': "This agent moved to another Host. Continue the conversation there.",
  'error.unknown': "The turn failed at {backend}. Try again, or switch model if it happens again.",
  'error.action.retry': "Try again",
  'error.action.retry_at': "Try again at {time}",
  'error.action.fresh_retry': "New session, try again",
  'error.action.compact_retry': "Compact and continue",
  'error.action.switch_model': "Switch backend or model",
  'error.action.sign_in': "Sign in again",
  'error.action.copy_details': "Copy diagnostics",
  'error.details': "Details",
  'error.copied': "Diagnostics copied",
  'error.retry_scheduled': "Will try again at {time}",
  'error.retry_sent': "Sent again",
  'error.action_failed': "That didn't work. Try again.",
  'error.repeat_count': { one: '{count}×', other: '{count}×' },
};

/** The app's language: the browser's first preferred one, mapped onto the
 * shipped languages (nb, nn, no -> nb; zh-Hans, zh-CN, zh-SG, zh -> zh-Hans;
 * anything else -> en). */
export function appLanguage(languages = (typeof navigator !== 'undefined' && (navigator.languages || [navigator.language])) || []) {
  const tag = String(languages[0] || '').replace(/_/g, '-').toLowerCase();
  const [lang, ...rest] = tag.split('-');
  if (['nb', 'nn', 'no'].includes(lang)) return 'nb';
  if (lang === 'zh' && (!rest.length || rest.some(p => ['hans', 'cn', 'sg'].includes(p)))) return 'zh-Hans';
  return ['hi', 'es', 'fr'].includes(lang) ? lang : 'en';
}

/** The catalog's string for `key` (the English source until translations land). */
export function tr(key, lang = appLanguage()) {
  void lang;
  return EN[key];
}

const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** `{time}`: the time alone today, with the weekday within a week, else with the date. */
export function formatTime(ms, lang = appLanguage(), now = Date.now()) {
  const at = new Date(ms);
  const today = new Date(now);
  if (!Number.isFinite(at.getTime())) return '';
  const midnight = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(at) - midnight(today)) / 86400000);
  const options = sameDay(at, today) ? { hour: 'numeric', minute: '2-digit' }
    : Math.abs(days) < 7 ? { weekday: 'short', hour: 'numeric', minute: '2-digit' }
    : at.getFullYear() === today.getFullYear() ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  return new Intl.DateTimeFormat(lang, options).format(at);
}

/** `template` filled from `params` (`{time}` from `time_ms`); null when a
 * placeholder it uses is missing or empty. */
export function fill(template, params = {}, lang = appLanguage(), now = Date.now()) {
  let missing = false;
  const out = String(template).replace(/\{(\w+)\}/g, (_, name) => {
    const value = name === 'time'
      ? (params.time_ms ? formatTime(params.time_ms, lang, now) : '')
      : params[name] == null ? '' : String(params[name]);
    if (!value.trim()) missing = true;
    return value;
  });
  return missing ? null : out;
}

/** A catalog string by key with `params`, or null (unknown key, empty placeholder). */
export function say(key, params = {}, lang = appLanguage(), now = Date.now()) {
  const template = tr(key, lang);
  return typeof template === 'string' ? fill(template, params, lang, now) : null;
}

/** The Host's own words: Norwegian when the app speaks it, else English. */
export function hostWords(words, lang = appLanguage()) {
  if (!words) return '';
  if (typeof words === 'string') return words;
  return String((lang === 'nb' ? words.nb || words.en : words.en || words.nb) || '');
}

/** The card's sentence: the catalog's by key, else the Host's text. */
export function errorSentence(error, lang = appLanguage(), now = Date.now()) {
  return (error.key && say(error.key, error.params || {}, lang, now)) || hostWords(error.text, lang);
}

/** A button's words (`error.action.<id>`, `{time}` its `at_ms`), else the Host's label. */
export function actionLabel(action, lang = appLanguage(), now = Date.now()) {
  return say(`error.action.${action.id}`, action.at_ms ? { time_ms: action.at_ms } : {}, lang, now) || hostWords(action.label, lang);
}

/** `error.repeat_count` for `count`, by the language's plural rules. */
export function repeatCount(count, lang = appLanguage()) {
  const forms = tr('error.repeat_count', lang);
  const form = forms[new Intl.PluralRules(lang).select(count)] || forms.other;
  return form.replace('{count}', String(count));
}
