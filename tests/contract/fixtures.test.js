import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  applyLog, applySnapshot, beginFetch, blankSync, endFetch, isClipReplay,
  onEvent, onOpen, pickClipSource,
} from '../../static/lib/conversation-sync.js';
import { AgentState } from '../../static/lib/protocol.js';
import {
  confirmFromTurns, createDeliveryLog, DeliveryState, markState,
  pendingIds, recordSend, staleSends,
} from '../../static/lib/delivery.js';

// Golden fixtures: JSON scenarios of inputs plus the expected end state,
// shared with the iOS CoreBehaviorTests target (contract/ios/sync-fixtures.sh).
// Fixture payloads are validated against contract/schemas by
// tests/contract/test_fixtures_validate.py so they cannot describe a
// server that does not exist; this runner only checks client behaviour.

const here = path.dirname(fileURLToPath(import.meta.url));
const fixturesDir = path.join(here, '..', '..', 'contract', 'fixtures');

function loadFixtures() {
  const out = [];
  for (const area of fs.readdirSync(fixturesDir).sort()) {
    const dir = path.join(fixturesDir, area);
    if (!fs.statSync(dir).isDirectory()) continue;
    for (const file of fs.readdirSync(dir).sort()) {
      if (!file.endsWith('.json')) continue;
      const body = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
      out.push({ name: `${area}/${file}`, title: body.title, body });
    }
  }
  return out;
}

const NOW = 1000000;

function runFixture(fx) {
  let sync = blankSync('rachel');
  let delivery = createDeliveryLog();
  const effects = [];
  const clipSources = [];
  const replays = [];
  let stale = [];
  const currentTurns = () => sync.order.map((id) => sync.turns[id]);
  for (const step of fx.steps) {
    if (step.open !== undefined) {
      const r = onOpen(sync); sync = r.state; effects.push(...r.effects);
    } else if (step.snapshot !== undefined) {
      const r = applySnapshot(sync, step.snapshot); sync = r.state; effects.push(...r.effects);
    } else if (step.log !== undefined) {
      const r = applyLog(sync, step.log.response, step.log.mode); sync = r.state; effects.push(...r.effects);
    } else if (step.sse !== undefined) {
      const r = onEvent(sync, step.sse); sync = r.state; effects.push(...r.effects);
    } else if (step.beginFetch !== undefined) {
      const r = beginFetch(sync, step.beginFetch); sync = r.state; effects.push(...r.effects);
    } else if (step.endFetch !== undefined) {
      const r = endFetch(sync); sync = r.state; effects.push(...r.effects);
    } else if (step.send !== undefined) {
      const s = step.send;
      const at = s.at ?? NOW;
      // A retry re-posts the same id; it must not open a second delivery entry.
      if (!s.retry) recordSend(delivery, { id: s.client_msg_id, session: s.session || 'rachel', text: s.text || '', at });
      markState(delivery, s.client_msg_id, DeliveryState.SENT, '', at + 1);
    } else if (step.confirm !== undefined) {
      confirmFromTurns(delivery, currentTurns(), NOW + 5000);
    } else if (step.stale !== undefined) {
      stale = staleSends(delivery, { now: step.stale.now, timeoutMs: step.stale.timeoutMs }).map((e) => e.id);
    } else if (step.clip !== undefined) {
      clipSources.push(pickClipSource(step.clip));
    } else if (step.replayCheck !== undefined) {
      replays.push(isClipReplay(step.replayCheck.clip_id, step.replayCheck.seen));
    } else {
      throw new Error(`unknown fixture step ${JSON.stringify(step)}`);
    }
  }
  return { sync, delivery, effects, clipSources, replays, stale };
}

describe.each(loadFixtures())('$name: $title', ({ body }) => {
  it('ends in the expected state', () => {
    const { sync, delivery, effects, clipSources, replays, stale } = runFixture(body);
    const ex = body.expect;
    expect(effects).toEqual(ex.effects ?? []);
    if ('cursor' in ex) expect(sync.cursor).toBe(ex.cursor);
    if ('turn_ids' in ex) expect(sync.order).toEqual(ex.turn_ids);
    if ('texts' in ex) expect(sync.order.map((id) => sync.turns[id].text)).toEqual(ex.texts);
    if ('missing' in ex) expect(sync.missing).toBe(ex.missing);
    if ('hasMore' in ex) expect(sync.hasMore).toBe(ex.hasMore);
    if ('clip_sources' in ex) expect(clipSources).toEqual(ex.clip_sources);
    if ('replays' in ex) expect(replays).toEqual(ex.replays);
    if ('delivered' in ex) {
      const got = delivery.entries.filter((e) => e.state === DeliveryState.CONFIRMED).map((e) => e.id);
      expect(got).toEqual(ex.delivered);
    }
    if ('stale' in ex) expect(stale).toEqual(ex.stale);
    if ('pending' in ex) expect([...pendingIds(delivery)]).toEqual(ex.pending);
    if ('busy' in ex) expect([...AgentState.BUSY].sort()).toEqual([...ex.busy].sort());
    if ('notBusy' in ex) {
      for (const s of ex.notBusy) expect(AgentState.BUSY.has(s)).toBe(false);
    }
  });
});
