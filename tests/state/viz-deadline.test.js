import {it,expect,vi,afterEach} from 'vitest';
import {FrameDeadline} from '../../static/lib/viz-deadline.js';
afterEach(()=>vi.useRealTimers());
it('allows a cheap frame delayed longer than the old 150ms transport limit',()=>{
 vi.useFakeTimers();const fail=vi.fn();const deadline=new FrameDeadline(fail,{isHidden:()=>false});
 deadline.arm(1);vi.advanceTimersByTime(500);deadline.clear();vi.advanceTimersByTime(4000);
 expect(fail).not.toHaveBeenCalled();
});
it('still terminates a non-returning visible worker within a bounded deadline',()=>{
 vi.useFakeTimers();const fail=vi.fn();const deadline=new FrameDeadline(fail,{isHidden:()=>false});
 deadline.arm(7);vi.advanceTimersByTime(3001);expect(fail).toHaveBeenCalledExactlyOnceWith(7);
});
it('does not punish browser suspension and rearms on return to the tab',()=>{
 vi.useFakeTimers();let hidden=false;const fail=vi.fn();const deadline=new FrameDeadline(fail,{isHidden:()=>hidden});
 deadline.arm(3);vi.advanceTimersByTime(100);hidden=true;deadline.visibilityChanged();
 vi.advanceTimersByTime(60000);expect(fail).not.toHaveBeenCalled();
 hidden=false;deadline.visibilityChanged();vi.advanceTimersByTime(2999);expect(fail).not.toHaveBeenCalled();
 vi.advanceTimersByTime(2);expect(fail).toHaveBeenCalledExactlyOnceWith(3);
});
it('an old cancelled request cannot terminate the next frame',()=>{
 vi.useFakeTimers();const fail=vi.fn();const deadline=new FrameDeadline(fail,{isHidden:()=>false});
 deadline.arm(1);vi.advanceTimersByTime(1000);deadline.arm(2);vi.advanceTimersByTime(2001);
 expect(fail).not.toHaveBeenCalled();deadline.clear();
});
