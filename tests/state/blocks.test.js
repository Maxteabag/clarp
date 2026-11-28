// Code blocks and tables in a chat message (web/src/lib/blocks.js).
import { describe, expect, it } from 'vitest';
import {
  codeBlockHtml, fenceLanguage, isTap, languageName, tableBlockHtml, tableToMarkdown,
} from '../../web/src/lib/blocks.js';

describe('code block header row', () => {
  it('names the language, has Copy and Expand, and keeps the code as written', () => {
    const html = codeBlockHtml('fn main() {\n    run();\n}\n', 'rust ignore');
    expect(html).toContain('<span class="blk-lang">Rust</span>');
    expect(html).toContain('class="language-rust"');
    expect(html).toContain('fn main() {\n    run();\n}</code>');
    expect(html).toContain('aria-label="Copy code"');
    expect(html).toContain('aria-label="Expand"');
  });

  it('says Code when the fence names no language', () => {
    expect(fenceLanguage('')).toBe('');
    expect(codeBlockHtml('<b>', '')).toContain('<span class="blk-lang">Code</span>');
    expect(codeBlockHtml('<b>', '')).toContain('&lt;b&gt;');
  });

  it('writes language names the way people do', () => {
    expect(languageName('py')).toBe('Python');
    expect(languageName('SH')).toBe('Shell');
    expect(languageName('cpp')).toBe('C++');
    expect(languageName('elm')).toBe('Elm');
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
