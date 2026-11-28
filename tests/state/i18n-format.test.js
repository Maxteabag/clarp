// Lengths of time in each shipped language (web/src/lib/i18n/format.js formatDuration): each
// language's own unit width, and no list comma between the units.

import { describe, expect, it } from 'vitest';
import { formatDuration } from '../../web/src/lib/i18n/format.js';

describe('formatDuration', () => {
  const cases = {
    en: ['1m 34s', '1h 2m', '6s'],
    nb: ['1 min 34 sek', '1 t 2 min', '6 sek'],
    'zh-Hans': ['1分钟34秒', '1小时2分钟', '6秒'],
    hi: ['1 मिनट 34 सेकंड', '1 घंटा 2 मिनट', '6 सेकंड'],
    es: ['1 min 34 s', '1 h 2 min', '6 s'],
    fr: ['1 min 34 s', '1 h 2 min', '6 s'],
  };
  for (const [lang, want] of Object.entries(cases)) {
    it(`writes ${lang} the language's way`, () => {
      // fr puts a no-break space inside some units, by ICU version; the test reads it as a space.
      const shown = [94000, 3720000, 6000].map(ms => formatDuration(ms, undefined, lang).replace(/[\u00a0\u202f]/g, ' '));
      expect(shown).toEqual(want);
    });
  }

  it('leaves out a zero unit after the first and keeps a lone zero', () => {
    expect(formatDuration(120000, undefined, 'en')).toBe('2m');
    expect(formatDuration(400, undefined, 'en')).toBe('0s');
  });
});
