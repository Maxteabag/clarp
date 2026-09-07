// Avatar selection shared by every PWA surface.
//
// Built-in agents use bundled images. User-created agents instead expose an
// authenticated, content-versioned avatar_url in /agents/snapshot. Prefer the
// route tied to the exact session so duplicate display names cannot borrow
// another agent's portrait.
//
// With `preferModel`, an agent still wearing its bundled persona image wears
// the variant drawn for the model behind it instead, where the server has
// one bundled (`model_avatar_url`).

export function avatarSlug(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

const BUNDLED_AVATAR_SLUGS = new Set([
  'adam', 'antoni', 'arnold', 'bella', 'caleb', 'diego', 'domi', 'elli',
  'freya', 'josh', 'lena', 'marcus', 'mike', 'nadia', 'omar', 'priya',
  'rachel', 'sam', 'theo', 'yuki',
]);

export function resolveAvatarUrl(agentsBySession, name, session = '',
                                 { preferModel = false } = {}) {
  const agents = agentsBySession || {};
  const exact = session ? agents[session] : null;
  // A model portrait only ever stands in for the bundled persona image, so
  // the server leaves avatar_url empty whenever it offers one. Preferring it
  // here can never hide an uploaded or generated portrait.
  if (preferModel && exact && exact.model_avatar_url) return exact.model_avatar_url;
  if (exact && exact.avatar_url) return exact.avatar_url;

  if (!session) {
    const named = Object.values(agents).find(agent =>
      agent && agent.name === name && (agent.avatar_url
        || (preferModel && agent.model_avatar_url)));
    if (named) {
      return (preferModel && named.model_avatar_url) || named.avatar_url;
    }
  }

  const slug = avatarSlug(name);
  return BUNDLED_AVATAR_SLUGS.has(slug)
    ? `/static/avatars/${slug}.png`
    : '';
}
