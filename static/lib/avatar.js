// Avatar selection shared by every PWA surface.
//
// Built-in agents use bundled images. User-created agents instead expose an
// authenticated, content-versioned avatar_url in /agents/snapshot. Prefer the
// route tied to the exact session so duplicate display names cannot borrow
// another agent's portrait.

export function avatarSlug(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

const BUNDLED_AVATAR_SLUGS = new Set([
  'adam', 'antoni', 'arnold', 'bella', 'caleb', 'diego', 'domi', 'elli',
  'freya', 'josh', 'lena', 'marcus', 'mike', 'nadia', 'omar', 'priya',
  'rachel', 'sam', 'theo', 'yuki',
]);

export function resolveAvatarUrl(agentsBySession, name, session = '') {
  const agents = agentsBySession || {};
  const exact = session ? agents[session] : null;
  if (exact && exact.avatar_url) return exact.avatar_url;

  if (!session) {
    const named = Object.values(agents).find(agent =>
      agent && agent.name === name && agent.avatar_url);
    if (named) return named.avatar_url;
  }

  const slug = avatarSlug(name);
  return BUNDLED_AVATAR_SLUGS.has(slug)
    ? `/static/avatars/${slug}.png`
    : '';
}
