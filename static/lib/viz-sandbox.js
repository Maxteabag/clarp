// Compile once per entity revision/state, asynchronously. Main-thread frames
// see only cached primitives or a placeholder. At most two workers exist.
export class DrawingCache {
  constructor() { this.cache = new Map(); this.queue = []; this.active = 0; }
  prepare(id, revision, program, state) {
    const key = JSON.stringify([id, revision, state.events]);
    if (this.cache.has(key)) return this.cache.get(key);
    const entry = {status:'pending', commands:[]};
    this.cache.set(key, entry);
    // Polling windows should not accumulate old entity revisions forever.
    if (this.cache.size > 512) this.cache.delete(this.cache.keys().next().value);
    this.queue.push({entry, program, state});
    this.pump();
    return entry;
  }
  pump() {
    while (this.active < 2 && this.queue.length) {
      const {entry, program, state} = this.queue.shift();
      this.active++;
      let worker, timer, finished = false;
      const done = result => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        worker?.terminate();
        entry.status = result?.failed ? 'failed' : 'ready';
        entry.commands = result?.commands || [];
        this.active--;
        this.pump();
      };
      try {
        worker = new Worker('/static/lib/viz-drawing-worker.js', {type:'module'});
        timer = setTimeout(() => done({failed:true}), 1000);
        worker.onmessage = e => done(e.data);
        worker.onerror = e => { e.preventDefault(); done({failed:true}); };
        worker.postMessage({program, state});
      } catch { done({failed:true}); }
    }
  }
}
