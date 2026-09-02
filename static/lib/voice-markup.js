const SPEAK_TAG_RE = /<\/?speak\b[^>]*>/gi;
const VOX_BLOCK_RE = /<vox\b[^>]*>[\s\S]*?<\/vox>/gi;
const VOICE_SSML_RE = /<\/?(?:break|speed|volume|emotion)\b[^>]*\/?>/gi;
const VOICE_SENTINEL = '\uE000';
const VOX_BOUNDARY_RE = /[ \t]*[,;:—–-]?[ \t]*\uE000[ \t]*[,.;:!?…—–-]?[ \t]*/g;
const LEADING_VOX_RE = /(^[ \t]*|[.!?][ \t]+|\n[ \t]*)\uE000[ \t]*([a-z])/g;
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

export function stripVoiceMarkup(value, { streaming = false } = {}) {
  const source = streaming ? hideIncompleteStreamingVoiceMarkup(value) : String(value);
  return source
    .replace(SPEAK_TAG_RE, '')
    .replace(VOICE_SSML_RE, '')
    .replace(VOX_BLOCK_RE, VOICE_SENTINEL)
    .replace(VOX_BOUNDARY_RE, ` ${VOICE_SENTINEL} `)
    .replace(LEADING_VOX_RE, (_, prefix, letter) => prefix + letter.toUpperCase())
    .replaceAll(VOICE_SENTINEL, ' ')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]+([,.;:!?])/g, '$1')
    .trim();
}
