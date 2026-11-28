// The framework-free half of the web app's localization: which languages ship, how a device
// language maps onto one of them, and how a catalog entry becomes the text shown. The reactive
// half (the current language, the setting) is ../../stores/i18n.svelte.js. See docs/i18n.md.

/** Shipped languages, BCP-47. English is the base every other catalog falls back to. */
export const LANGUAGES = ['en', 'nb', 'zh-Hans', 'hi', 'es', 'fr'];

/** Each language in its own name. Shown as is in the Language setting, never translated. */
export const ENDONYMS = {
  en: 'English',
  nb: 'Norsk bokmål',
  'zh-Hans': '简体中文',
  hi: 'हिन्दी',
  es: 'Español',
  fr: 'Français',
};

/**
 * One device language tag onto a shipped language, or null when none fits.
 * nb / nn / no -> nb; zh-Hans, zh-CN, zh-SG and bare zh -> zh-Hans (Traditional Chinese is not
 * shipped, so zh-Hant / zh-TW / zh-HK / zh-MO find nothing here).
 */
export function matchLanguage(tag) {
  if (!tag || typeof tag !== 'string') return null;
  const parts = tag.toLowerCase().replace(/_/g, '-').split('-');
  const lang = parts[0];
  if (lang === 'nb' || lang === 'nn' || lang === 'no') return 'nb';
  if (lang === 'zh') {
    if (parts.includes('hans')) return 'zh-Hans';
    if (parts.some(p => p === 'hant' || p === 'tw' || p === 'hk' || p === 'mo')) return null;
    return 'zh-Hans';
  }
  return ['en', 'hi', 'es', 'fr'].includes(lang) ? lang : null;
}

/** The device's preferred languages, in order, onto the first shipped one; English otherwise. */
export function systemLanguage(preferred) {
  for (const tag of preferred || []) {
    const hit = matchLanguage(tag);
    if (hit) return hit;
  }
  return 'en';
}

/** 'system' or a shipped language: the setting resolved to the language to show. */
export function resolveLanguage(choice, preferred) {
  return LANGUAGES.includes(choice) ? choice : systemLanguage(preferred);
}

const pluralRules = new Map();
function pluralCategory(locale, count) {
  let rules = pluralRules.get(locale);
  if (!rules) { rules = new Intl.PluralRules(locale); pluralRules.set(locale, rules); }
  return rules.select(count);
}

const numberFormats = new Map();
export function numberFormat(locale) {
  let f = numberFormats.get(locale);
  if (!f) { f = new Intl.NumberFormat(locale); numberFormats.set(locale, f); }
  return f;
}

/**
 * The text for `key` in `dict` (falling back to `base`, then to the key itself), with `{name}`
 * placeholders filled from `params`. A plural entry is an object of CLDR categories
 * ({one, other}, plus few/many where a language has them) chosen by `params.count`. Numbers are
 * written with the locale's digits and separators.
 */
export function translate(locale, dict, base, key, params) {
  let msg = dict?.[key] ?? base?.[key];
  if (msg == null) return key;
  if (typeof msg === 'object') {
    const count = Number(params?.count ?? 0);
    msg = msg[pluralCategory(locale, count)] ?? msg.other ?? '';
  }
  if (!params) return msg;
  return msg.replace(/\{(\w+)\}/g, (whole, name) => {
    const v = params[name];
    if (v == null) return whole;
    return typeof v === 'number' ? numberFormat(locale).format(v) : String(v);
  });
}
