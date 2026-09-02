import { describe, expect, it } from 'vitest';
import { isLoggableActivity } from '../../static/lib/activity-view-model.js';

describe('activity view model', () => {
  it('keeps transient state rows out of the durable activity timeline', () => {
    expect(isLoggableActivity({ session: 'mike', kind: 'thinking', summary: 'Working' }, 'mike')).toBe(false);
    expect(isLoggableActivity({ session: 'mike', kind: 'done', summary: 'Done' }, 'mike')).toBe(false);
  });

  it('keeps real tool activity for the focused conversation', () => {
    expect(isLoggableActivity({ session: 'mike', kind: 'tool', tool: 'Bash' }, 'mike')).toBe(true);
    expect(isLoggableActivity({ session: 'rachel', kind: 'tool', tool: 'Bash' }, 'mike')).toBe(false);
  });
});
