const SPEAK_TAG_RE = /<\/?speak\b[^>]*>/gi;
const VOX_CONTENT_RE = /<vox\b[^>]*>([\s\S]*?)<\/vox>/gi;
const VOX_EDGE_RE = /^([\s,.;:!?…—–-]*)([\s\S]*?)([\s,.;:!?…—–-]*)$/;
const VOICE_SSML_RE = /<\/?(?:break|speed|volume|emotion)\b[^>]*\/?>/gi;
// Bracketed emotion tags Gemini TTS acts out ([laughing], [sigh]). Dropped from
// display inside <speak>; mirrors server/lib/voice_markup.py.
const EMOTION_TAG_RE = /\[[a-z][a-z' -]{2,30}\](?![(:])/g;
const SPEAK_REGION_RE = /(<speak\b[^>]*>)([\s\S]*?)(<\/speak>|$)/gi;
const VOICE_SENTINEL = '\uE000';
const VOX_RUN_RE = /\uE000(?:[ \t]*[,;:—–]?[ \t]*\uE000)+/g;
// What may sit between the start of a line and a filler that opens a sentence:
// list and quote markers, a heading, an opening bold or italic marker.
const LINE_OPENING_RE = /^[ \t]*(?:(?:[-*+>]|\d+[.)]|#{1,6})[ \t]+)*(?:\*\*|__|\*|_)?[ \t]*$/;
const SOFT_MARKS = ',;:—–';
const END_MARKS = '.!?…';
const WORD_STOPS = '—–,;:!?…()[]"“”<';
const STREAMING_VOICE_TAG_PREFIXES = [
  '<speak', '</speak', '<vox', '</vox', '<break', '</break',
  '<speed', '</speed', '<volume', '</volume', '<emotion', '</emotion>',
];

export function hideIncompleteStreamingVoiceMarkup(value) {
  let text = String(value || '');
  let lower = text.toLowerCase();
  const openVox = lower.lastIndexOf('<vox');
  const closeVox = lower.lastIndexOf('</vox>');
  if (openVox > closeVox) text = text.slice(0, openVox);

  lower = text.toLowerCase();
  const marker = lower.lastIndexOf('<');
  if (marker < 0) return text;
  const tail = lower.slice(marker);
  if (tail.includes('>')) return text;
  if (STREAMING_VOICE_TAG_PREFIXES.some(prefix =>
    prefix.startsWith(tail) || tail.startsWith(prefix))) {
    return text.slice(0, marker);
  }
  return text;
}

const isBlank = (c) => c === ' ' || c === '\t';
const isLower = (c) => c !== c.toUpperCase() && c === c.toLowerCase();

function capitalizeSentence(rest) {
  // Only a plain lowercase word: "iOS", "npm" in backticks, file.py stay as written.
  let word = '';
  for (const c of rest) {
    if (/\s/.test(c) || WORD_STOPS.includes(c)) break;
    word += c;
  }
  word = word.replace(/[.'’*_]+$/, '');
  const chars = [...word];
  if (chars.length && isLower(chars[0]) && chars.every((c) => isLower(c) || "'’-".includes(c))) {
    return chars[0].toUpperCase() + rest.slice(chars[0].length);
  }
  return rest;
}

// Remove the filler marker at `i` with the punctuation that only it needed.
// Mirrors server/lib/voice_markup.py; contract/fixtures/voice-display.json is
// the contract every client runs.
function closeVoxGap(s, i) {
  let j = i;
  while (j > 0 && isBlank(s[j - 1])) j -= 1;
  let m = i + 1;
  while (m < s.length && isBlank(s[m])) m += 1;
  const space = j < i || m > i + 1 ? ' ' : '';
  const right = s[m] ?? '';
  let after = m + 1;
  let kind;
  if ((right && SOFT_MARKS.includes(right)) || (right === '-' && (after >= s.length || isBlank(s[after])))) {
    while (after < s.length && isBlank(s[after])) after += 1;
    kind = 'soft';
  } else if (right && END_MARKS.includes(right)) {
    kind = 'end-mark';
  } else if (right === '' || right === '\n') {
    kind = 'line-end';
  } else {
    kind = 'word';
  }
  const left = j > 0 ? s[j - 1] : '';
  const lineStart = s.lastIndexOf('\n', i - 1) + 1;
  if (LINE_OPENING_RE.test(s.slice(lineStart, i)) || (left && '.!?'.includes(left))) {
    // The filler opened a sentence: its comma or stop goes with it and the
    // next word starts the sentence.
    if (kind === 'end-mark') {
      while (after < s.length && (END_MARKS.includes(s[after]) || isBlank(s[after]))) after += 1;
    }
    const rest = kind === 'soft' || kind === 'end-mark' ? s.slice(after) : s.slice(m);
    const sep = j < i && rest !== '' && rest[0] !== '\n' ? ' ' : '';
    return s.slice(0, j) + sep + capitalizeSentence(rest);
  }
  if ((left && SOFT_MARKS.includes(left)) || (left === '-' && j > 1 && isBlank(s[j - 2]))) {
    if (kind === 'end-mark' || kind === 'line-end') {
      // A comma left dangling before a stop or a line end goes too.
      let k = j - 1;
      while (k > 0 && isBlank(s[k - 1])) k -= 1;
      return s.slice(0, k) + s.slice(m);
    }
    // The mark before the filler is the sentence's own; keep it and drop the
    // filler's second one.
    return s.slice(0, j) + ' ' + (kind === 'soft' ? s.slice(after) : s.slice(m));
  }
  const rest = kind === 'soft' && right === ',' ? s.slice(after) : s.slice(m);
  const sep = rest === '' || rest[0] === '\n' || ',;:.!?…'.includes(rest[0]) ? '' : space;
  return s.slice(0, j) + sep + rest;
}

function dropVoxForDisplay(text) {
  let s = text.replace(VOX_CONTENT_RE, (_, content) => {
    // Punctuation written inside the filler ("<vox>um,</vox>") belongs to
    // the sentence around it, so it moves outside the hidden part.
    const [, lead, core, trail] = VOX_EDGE_RE.exec(content);
    return core ? lead + VOICE_SENTINEL + trail : lead + trail;
  });
  if (!s.includes(VOICE_SENTINEL)) return s;
  s = s.replace(VOX_RUN_RE, VOICE_SENTINEL);
  for (let i = s.indexOf(VOICE_SENTINEL); i >= 0; i = s.indexOf(VOICE_SENTINEL)) {
    s = closeVoxGap(s, i);
  }
  return s;
}

export function stripVoiceMarkup(value, { streaming = false } = {}) {
  const source = streaming ? hideIncompleteStreamingVoiceMarkup(value) : String(value);
  return dropVoxForDisplay(source
    .replace(SPEAK_REGION_RE, (_, open, body, close) =>
      open + body.replace(EMOTION_TAG_RE, ' ') + close)
    .replace(SPEAK_TAG_RE, '')
    .replace(VOICE_SSML_RE, ''))
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]+([,.;:!?])/g, '$1')
    .trim();
}
