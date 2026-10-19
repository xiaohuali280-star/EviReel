import {describe, expect, it} from 'vitest';
import {isProcessing, seekTime, time} from './api';
import {matches} from './chat';

describe('navigation and scoped events', () => {
  it('defers unknown metadata and clamps real media targets', () => {
    expect(seekTime(5, NaN)).toBe(null);
    expect(seekTime(Infinity, 10)).toBe(null);
    expect(seekTime(-2, 10)).toBe(0);
    expect(seekTime(20, 10)).toBe(9.95);
    expect(time(3661)).toBe('1:01:01');
  });
  it('rejects a late result from a different video, conversation or request', () => {
    const p = {type: 'text' as const, video_id: 'v_a', conversation_id: 'c_a', request_id: 'r_a', text: 'question'};
    expect(matches({...p, type: 'final'}, p)).toBe(true);
    for (const key of ['video_id', 'conversation_id', 'request_id']) expect(matches({...p, [key]: 'other'}, p)).toBe(false);
  });
  it('keeps processing separate from partial usable capabilities', () => {
    expect(isProcessing('running')).toBe(true);
    expect(isProcessing('queued')).toBe(true);
    expect(isProcessing('partial')).toBe(false);
    expect(isProcessing('interrupted')).toBe(false);
  });
});
