import fs from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  hideIncompleteStreamingVoiceMarkup,
  stripVoiceMarkup,
} from '../../static/lib/voice-markup.js';

// Shared with the Host, desktop and iOS: the written text left once <vox>
// fillers are hidden.
const voiceDisplay = JSON.parse(fs.readFileSync(
  new URL('../../contract/fixtures/voice-display.json', import.meta.url), 'utf8'));

describe('voice display fixture', () => {
  it.each(voiceDisplay.cases.map((c) => [c.name, c]))('%s', (_, c) => {
    expect(stripVoiceMarkup(c.input, { streaming: !!c.streaming })).toBe(c.expect);
  });
});

describe('voice markup display cleanup', () => {
  it('repairs leading filler punctuation and capitalization', () => {
    const raw = '<speak><vox>Hmm</vox>, okay, let’s try this naturally. '
      + '<break time="350ms"/> I think it works. '
      + '<vox>You know</vox>, the words stay the same.</speak>';

    expect(stripVoiceMarkup(raw)).toBe(
      'Okay, let’s try this naturally. I think it works. The words stay the same.',
    );
  });

  it('drops paired and stray speed tags', () => {
    expect(stripVoiceMarkup('<speak><speed ratio="0.85">slow</speed> done</speak>')).toBe('slow done');
    expect(stripVoiceMarkup('ready </speed> now')).toBe('ready now');
  });

  it('hides every incomplete streaming voice-tag prefix', () => {
    for (const prefix of ['<', '<s', '<sp', '<spe', '<spea', '<speak', '</s', '<v', '<br']) {
      expect(stripVoiceMarkup(`Ready ${prefix}`, { streaming: true })).toBe('Ready');
    }
  });

  it('hides unfinished audio-only vox content', () => {
    expect(stripVoiceMarkup('Ready <vox>um', { streaming: true })).toBe('Ready');
  });

  it('preserves ordinary less-than text and final incomplete text', () => {
    expect(hideIncompleteStreamingVoiceMarkup('2 < 3')).toBe('2 < 3');
    expect(stripVoiceMarkup('literal <s')).toBe('literal <s');
  });

  it('hides Gemini emotion tags inside speak but keeps links and checkboxes', () => {
    expect(stripVoiceMarkup(
      '<speak>Oh <vox>[laughing]</vox> that worked. [sigh] Finally.</speak> See [docs](https://x.y) [x]',
    )).toBe('Oh that worked. Finally. See [docs](https://x.y) [x]');
  });
});
