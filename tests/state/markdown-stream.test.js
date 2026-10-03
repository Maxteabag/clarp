// Streaming text without re-parsing the world: the text splits into
// committed blocks (closed by a blank line or a closing fence, never inside
// an open fence) that render once, and a tail that is the only part
// re-rendered per update. The reveal is paced so a burst drains smoothly in
// about 200 ms instead of landing in jumps.

import { describe, expect, it } from 'vitest';
import { createBlockCache, revealStep, splitCommitted } from '@core/markdown-stream.js';

describe('splitCommitted', () => {
  it('commits paragraphs closed by a blank line and keeps the open one as the tail', () => {
    expect(splitCommitted('One.\n\nTwo.\n\nThr')).toEqual({
      committed: ['One.\n\n', 'Two.\n\n'], tail: 'Thr' });
  });

  it('never commits inside an open code fence', () => {
    const text = 'Intro.\n\n```js\nconst a = 1;\n\nconst b = 2;\n';
    expect(splitCommitted(text)).toEqual({
      committed: ['Intro.\n\n'], tail: '```js\nconst a = 1;\n\nconst b = 2;\n' });
  });

  it('commits a fence once it closes', () => {
    const text = '```\nx\n```\nAfter';
    expect(splitCommitted(text)).toEqual({ committed: ['```\nx\n```\n'], tail: 'After' });
  });

  it('puts the blocks back together into the same text', () => {
    const text = '# Title\n\nPara one\nstill one.\n\n- a\n- b\n\n~~~\ncode\n\n~~~\n\nend';
    const { committed, tail } = splitCommitted(text);
    expect(committed.join('') + tail).toBe(text);
  });

  it('holds a table in the tail until a blank line closes it', () => {
    const text = 'Rows:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n';
    expect(splitCommitted(text).tail).toBe('| a | b |\n|---|---|\n| 1 | 2 |\n');
  });
});

describe('createBlockCache', () => {
  it('renders a committed block once however often the message grows', () => {
    let calls = 0;
    const cache = createBlockCache(s => { calls++; return `<p>${s.trim()}</p>`; });
    let text = 'One.\n\n';
    for (const more of ['Two', ' words', '.\n\nThree']) {
      text += more;
      splitCommitted(text).committed.forEach(b => cache.render(b));
    }
    expect(calls).toBe(2);
    expect(cache.render('One.\n\n')).toBe('<p>One.</p>');
  });

  it('forgets the oldest blocks past its size', () => {
    let calls = 0;
    const cache = createBlockCache(s => { calls++; return s; }, 2);
    cache.render('a'); cache.render('b'); cache.render('c'); cache.render('a');
    expect(calls).toBe(4);
  });
});

describe('revealStep', () => {
  const text = 'The failure came from an off-by-one in the tokenizer when the input ends with a newline.';

  it('drains a burst within about 200 ms of 16 ms frames', () => {
    let shown = 0;
    let frames = 0;
    while (shown < text.length && frames < 100) { shown = revealStep(text, shown, 16); frames++; }
    expect(shown).toBe(text.length);
    expect(frames).toBeLessThanOrEqual(14);
  });

  it('stops on word boundaries until the end', () => {
    let shown = 0;
    while (shown < text.length) {
      shown = revealStep(text, shown, 16);
      if (shown < text.length) expect(text[shown]).toMatch(/\s/);
    }
  });

  it('never goes backwards and is done when everything is shown', () => {
    expect(revealStep(text, text.length, 16)).toBe(text.length);
    expect(revealStep('abc', 10, 16)).toBe(3);
  });
});
