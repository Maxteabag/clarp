// Streams (requests and tunneled WebSockets) one Host may have open at once. Anyone who knows
// a host id can open streams before the Host checks them; past this the relay answers 429.
// Kept out of the Worker's main module: the runtime takes every export there for a handler and
// refuses to start on a constant.
export const MAX_STREAMS = 256;
