// The app's language: 'System' (the browser's preferred languages) or the one picked under
// Settings > Language, kept in localStorage on this device. t(key, params) reads the current
// catalog, so text in a component re-renders the moment the language changes. See docs/i18n.md.

import en from '../lib/i18n/en.json';
import { LANGUAGES, resolveLanguage, translate } from '../lib/i18n/core.js';

const KEY = 'clarpLanguage';

// Non-English catalogs load when first used. The build keeps one bundle (vite.config.js), so
// they ride inside it, but each is only parsed when that language is shown.
const catalogs = import.meta.glob(['../lib/i18n/*.json', '!../lib/i18n/en.json', '!../lib/i18n/comments.json'],
  { import: 'default' });

function savedChoice() {
  try {
    const v = localStorage.getItem(KEY);
    return LANGUAGES.includes(v) ? v : 'system';
  } catch (_) {
    return 'system';
  }
}

function preferred() {
  return typeof navigator !== 'undefined' ? (navigator.languages?.length ? navigator.languages : [navigator.language]) : [];
}

export const i18n = $state({
  /** 'system' or a language from LANGUAGES: what the setting says. */
  choice: savedChoice(),
  /** The language shown, BCP-47 ('en', 'nb', 'zh-Hans', ...). Pass to Intl and lib/i18n/format.js. */
  locale: 'en',
});

// The catalog for i18n.locale; raw because it is replaced whole and never edited in place.
let dict = $state.raw(en);

let applying = 0;
async function apply() {
  const ticket = ++applying;
  const locale = resolveLanguage(i18n.choice, preferred());
  let next = en;
  if (locale !== 'en') {
    try {
      next = await catalogs[`../lib/i18n/${locale}.json`]();
    } catch (_) {
      next = en;
    }
  }
  if (ticket !== applying) return;  // a later choice won the race
  dict = next;
  i18n.locale = locale;
  if (typeof document !== 'undefined') document.documentElement.lang = locale;
}

/** Load the language before the app mounts, and follow the browser's when on 'System'. */
export async function initI18n() {
  if (typeof window !== 'undefined') {
    window.addEventListener('languagechange', () => { if (i18n.choice === 'system') apply(); });
  }
  await apply();
}

/** The Language setting: 'system' or one of LANGUAGES. Applies at once and is kept on this device. */
export function setLanguage(choice) {
  i18n.choice = LANGUAGES.includes(choice) ? choice : 'system';
  try {
    if (i18n.choice === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, i18n.choice);
  } catch (_) {}
  return apply();
}

/** The text for `key` in the current language; `{name}` filled from params, plurals by params.count. */
export function t(key, params) {
  return translate(i18n.locale, dict, en, key, params);
}
