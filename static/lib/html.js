export function escapeHTML(value) {
  return String(value).replace(/[&<>]/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
  }[ch]));
}

export function escapeAttr(value) {
  return escapeHTML(value).replace(/["']/g, ch => ({
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}
