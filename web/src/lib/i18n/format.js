// Locale-aware dates, times, relative times and numbers for the web app. Never hand-build
// weekday names, "yesterday", "ago" or separators; call one of these. Each takes the language
// to write in as its last argument and defaults to the app's current one (stores/i18n.svelte.js),
// so a component re-renders when the language changes. Times are epoch ms, a Date, or an ISO string.

import { i18n } from '../../stores/i18n.svelte.js';
import { numberFormat } from './core.js';

function toDate(t) {
  return t instanceof Date ? t : new Date(t);
}

const cache = new Map();
function dtf(locale, opts) {
  const k = locale + JSON.stringify(opts);
  let f = cache.get(k);
  if (!f) { f = new Intl.DateTimeFormat(locale, opts); cache.set(k, f); }
  return f;
}

/** 28 Nov 2026 / 28. nov. 2026 / 2026年11月28日. `opts` overrides the Intl options. */
export function formatDate(t, opts = { dateStyle: 'medium' }, locale = i18n.locale) {
  return dtf(locale, opts).format(toDate(t));
}

/** 14:05 / 2:05 PM, as the language writes it. */
export function formatTime(t, opts = { timeStyle: 'short' }, locale = i18n.locale) {
  return dtf(locale, opts).format(toDate(t));
}

/** Date and time together. */
export function formatDateTime(t, opts = { dateStyle: 'medium', timeStyle: 'short' }, locale = i18n.locale) {
  return dtf(locale, opts).format(toDate(t));
}

/** The weekday's name: Thursday / torsdag / 星期四. */
export function formatWeekday(t, style = 'long', locale = i18n.locale) {
  return dtf(locale, { weekday: style }).format(toDate(t));
}

/**
 * A chat-list style stamp: the time today, the weekday within the last six days, the date
 * before that.
 */
export function formatStamp(t, now = Date.now(), locale = i18n.locale) {
  const d = toDate(t);
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  if (d >= today) return formatTime(d, undefined, locale);
  if (today - d < 6 * 86400000) return formatWeekday(d, 'long', locale);
  return formatDate(d, { day: 'numeric', month: 'short', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' }, locale);
}

const UNITS = [
  ['year', 365 * 86400], ['month', 30 * 86400], ['week', 7 * 86400],
  ['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1],
];

/**
 * How long ago (or until): "now", "5 minutes ago", "yesterday", "i går", "in 2 hours".
 * `style` is Intl's 'long' | 'short' | 'narrow'.
 */
export function formatRelative(t, now = Date.now(), style = 'long', locale = i18n.locale) {
  const secs = Math.round((toDate(t).getTime() - now) / 1000);
  const abs = Math.abs(secs);
  const rtf = relativeFormat(locale, style);
  if (abs < 45) return rtf.format(0, 'second');
  const [unit, size] = UNITS.find(([, s]) => abs >= s);
  return rtf.format(Math.trunc(secs / size), unit);
}

/**
 * A wait counted down: "in 5 seconds", "in 2 minutes", "om 30 sekunder". Seconds stay seconds
 * (formatRelative would say "now" under 45 s); a minute or more is whole minutes.
 */
export function formatCountdown(ms, style = 'long', locale = i18n.locale) {
  const secs = Math.max(1, Math.ceil(ms / 1000));
  const rtf = relativeFormat(locale, style, 'always');
  return secs < 60 ? rtf.format(secs, 'second') : rtf.format(Math.round(secs / 60), 'minute');
}

const rtfs = new Map();
function relativeFormat(locale, style, numeric = 'auto') {
  const k = locale + style + numeric;
  let f = rtfs.get(k);
  if (!f) { f = new Intl.RelativeTimeFormat(locale, { numeric, style }); rtfs.set(k, f); }
  return f;
}

/** 12,345 / 12 345 / 12.345, as the language writes it. `opts` gives other Intl options. */
export function formatNumber(n, opts, locale = i18n.locale) {
  return opts ? new Intl.NumberFormat(locale, opts).format(n) : numberFormat(locale).format(n);
}

/** The CLDR plural category of `count` ('one', 'other', ...). For text use t(key, {count}). */
export function pluralCategory(count, locale = i18n.locale) {
  return new Intl.PluralRules(locale).select(count);
}

const durationFormats = new Map();
/**
 * A length of time: "4s", "1m 5s", "1h 2m" in English, the language's own units elsewhere
 * (`style` 'narrow' | 'short' | 'long'). Under a minute counts seconds, under an hour minutes
 * and seconds, otherwise hours and minutes. Uses Intl.DurationFormat where the browser has it,
 * else each unit through Intl.NumberFormat.
 */
export function formatDuration(ms, style = 'narrow', locale = i18n.locale) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const m = Math.floor(s / 60);
  const parts = s < 60 ? { seconds: s }
    : m < 60 ? { minutes: m, seconds: s % 60 }
    : { hours: Math.floor(m / 60), minutes: m % 60 };
  if (typeof Intl.DurationFormat === 'function') {
    const k = locale + style;
    let f = durationFormats.get(k);
    if (!f) { f = new Intl.DurationFormat(locale, { style }); durationFormats.set(k, f); }
    return f.format(parts);
  }
  const unit = { hours: 'hour', minutes: 'minute', seconds: 'second' };
  const shown = Object.entries(parts).filter(([, n]) => n);
  return (shown.length ? shown : [['seconds', 0]])
    .map(([u, n]) => new Intl.NumberFormat(locale, { style: 'unit', unit: unit[u], unitDisplay: style }).format(n))
    .join(' ');
}

/** "Mike, Rachel and Ida" / "Mike, Rachel og Ida": names joined as the language joins a list. */
export function formatList(items, type = 'conjunction', locale = i18n.locale) {
  return new Intl.ListFormat(locale, { type }).format(items);
}
