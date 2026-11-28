// Code blocks and tables in a chat message (web/src/lib/blocks.js).
import { describe, expect, it } from 'vitest';
import {
  codeBlockHtml, fenceLanguage, isTap, tableBlockHtml, tableToMarkdown,
} from '../../web/src/lib/blocks.js';

describe('code block head line', () => {
  it('names the fence language and keeps the code as written', () => {
    const html = codeBlockHtml('fn main() {\n    run();\n}\n', 'rust ignore');
    expect(html).toContain('<span class="blk-lang">rust</span>');
    expect(html).toContain('class="language-rust"');
    expect(html).toContain('fn main() {\n    run();\n}</code>');
    expect(html).toContain('aria-label="Copy code"');
  });

  it('shows no language when the fence names none', () => {
    expect(fenceLanguage('')).toBe('');
    expect(codeBlockHtml('<b>', '')).not.toContain('blk-lang');
    expect(codeBlockHtml('<b>', '')).toContain('&lt;b&gt;');
  });
});

describe('tap or drag', () => {
  it('opens on a short tap that barely moved', () => {
    expect(isTap({ dx: 3, dy: 2, ms: 120 })).toBe(true);
  });
  it('a sideways drag, a long press or selected text does not open', () => {
    expect(isTap({ dx: 40, dy: 0, ms: 120 })).toBe(false);
    expect(isTap({ dx: 0, dy: 0, ms: 700 })).toBe(false);
    expect(isTap({ dx: 0, dy: 0, ms: 100, selected: true })).toBe(false);
  });
});

describe('table source', () => {
  it('writes a parsed table back as Markdown with its alignment', () => {
    expect(tableToMarkdown(['a', 'b|c'], [['1', '2']], ['', 'right'])).toBe(
      '| a | b\\|c |\n| --- | ---: |\n| 1 | 2 |');
  });
  it('keeps the source on the block for copy', () => {
    expect(tableBlockHtml('<table></table>', '| a |\n|---|\n| "1" |\n'))
      .toContain('data-md="| a |\n|---|\n| &quot;1&quot; |"');
  });
});
