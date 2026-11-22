// Which Host a chat is on, as a small capsule on its row and in its header.
//
// The capsule shows only while the setting is on (the default) AND the client reads chats
// from more than one Host: with one Host there is nothing to tell apart, so it never shows.
// Its text is the client's name for that Host (the name the Host gives in /server-info).
// A moved row whose destination is not among the Hosts read names the destination.
import { movedHost } from './agent-moved.js';

/** Whether chats carry the capsule at all. */
export function hostBadgeShown(setting, hostCount) {
  return setting !== false && Number(hostCount) > 1;
}

/** The capsule's text for one chat, or '' (not shown).
 *  hosts: [{id, name}] the client reads; rowHost: the id of the Host the row came from;
 *  moved: the row's normalized `moved`. */
export function hostBadgeText({ setting = true, hosts = [], rowHost = '', moved = null } = {}) {
  if (!hostBadgeShown(setting, hosts.length)) return '';
  if (moved) {
    const destination = String(moved.to?.id || '');
    const listed = hosts.some(host => host.id && host.id === destination);
    const name = movedHost(moved);
    if (!listed && name) return name;
  }
  const own = hosts.find(host => host.id === rowHost) || (rowHost ? null : hosts[0]);
  return String(own?.name || '').trim();
}
