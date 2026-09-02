import { describe, expect, it } from 'vitest';
import {
  hideIncompleteStreamingVoiceMarkup,
  stripVoiceMarkup,
} from '../../static/lib/voice-markup.js';

describe('voice markup display cleanup', () => {
  it('repairs leading filler punctuation and capitalization', () => {
    const raw = '<speak><vox>Hmm</vox>, okay, let’s try this naturally. '
      + '<break time="350ms"/> I think it works. '
      + '<vox>You know</vox>, the words stay the same.</speak>';

    expect(stripVoiceMarkup(raw)).toBe(
      'Okay, let’s try this naturally. I think it works. The words stay the same.',
    );
  });

  it('repairs inline punctuation around a removed filler', () => {
    expect(stripVoiceMarkup('I, <vox>um</vox>, think so.')).toBe('I think so.');
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
});
