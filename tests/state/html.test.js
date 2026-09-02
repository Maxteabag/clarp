import { describe, expect, it } from 'vitest';
import { escapeAttr, escapeHTML } from '../../static/lib/html.js';

describe('html escaping', () => {
  it('keeps text escaping compact for element bodies', () => {
    expect(escapeHTML('<x&y>')).toBe('&lt;x&amp;y&gt;');
  });

  it('escapes quotes for attribute contexts', () => {
    expect(escapeAttr(`a"b'c<d>`)).toBe('a&quot;b&#39;c&lt;d&gt;');
  });
});
