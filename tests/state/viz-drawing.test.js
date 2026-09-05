import {describe, it, expect} from 'vitest';
import {evaluateDrawing, seedPosition} from '../../static/lib/viz-drawing.js';

describe('sandboxed map drawing', () => {
  it('compiles pure state into bounded drawing primitives', () => {
    expect(evaluateDrawing([{op:'circle', args:[0,0,['min', 'events', 2]]}], {events:8}))
      .toEqual([{op:'circle', args:[0,0,2]}]);
  });
  it('rejects thrown, executable, recursive, and non-finite generations', () => {
    for (const p of ['throw Error()', [{op:'eval', args:[]}], [{op:'circle',args:[0,0,Infinity]}],
      [{op:'circle',args:[0,0,'constructor']}], Array(65).fill({op:'line',args:[0,0,1,1]})]) {
      expect(() => evaluateDrawing(p)).toThrow();
    }
    let expr=1;
    for(let i=0;i<10;i++) expr=['add',1,expr];
    expect(() => evaluateDrawing([{op:'circle',args:[0,0,expr]}])).toThrow();
  });
  it('stops on the deadline', () => {
    let time=0;
    expect(() => evaluateDrawing([{op:'circle',args:[0,0,1]}], {}, () => time+=5)).toThrow('budget');
  });
  it('gives an identity the same starting position every time', () => {
    expect(seedPosition('repo:clarp', false)).toEqual(seedPosition('repo:clarp', false));
    expect(seedPosition('repo:clarp', false)).not.toEqual(seedPosition('service:clarp', false));
  });
});

import {vi} from 'vitest';
import {DrawingCache} from '../../static/lib/viz-sandbox.js';
it('terminates a nonresponsive worker and caches its placeholder', () => {
  vi.useFakeTimers();
  const terminate=vi.fn();
  vi.stubGlobal('Worker',class {postMessage(){} terminate(){terminate();}});
  try{
    const cache=new DrawingCache();
    const entry=cache.prepare('novel',1,[],{events:1});
    expect(entry.status).toBe('pending');
    vi.advanceTimersByTime(1001);
    expect(entry.status).toBe('failed');
    expect(terminate).toHaveBeenCalledOnce();
    expect(cache.prepare('novel',1,[],{events:1})).toBe(entry);
  }finally{vi.unstubAllGlobals();vi.useRealTimers();}
});
