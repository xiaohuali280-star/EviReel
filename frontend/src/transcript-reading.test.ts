import {describe, expect, it} from 'vitest';
import {groupTranscript, isSpeaking, ParagraphBuilder, readMode, sourceId, targetDuration} from './transcript-reading';
import type {Transcript} from './types';
const segment = (n: number, text = `第${n}句。`, start = n * 3, end = start + 3): Transcript =>
  ({segment_id: `s-${n}`, transcript_version: 'v1', start_t: start, end_t: end, text});

describe('reading density and source preservation', () => {
  it('adapts continuously with safe limits and invalid-duration defaults', () => {
    expect([120, 480, 1800, 3600].map(targetDuration)).toEqual([12, 22, 43, 45]);
    for (const n of [0, -1, NaN, Infinity]) expect(targetDuration(n)).toBe(22);
    expect(targetDuration(479)).toBe(targetDuration(480));
  });
  it('merges 154 short segments into readable paragraphs without changing any source', () => {
    const sources = Array.from({length: 154}, (_, i) => segment(i));
    const groups = groupTranscript(sources, 480);
    expect(groups.length).toBeGreaterThanOrEqual(20); expect(groups.length).toBeLessThanOrEqual(30);
    expect(groups.flatMap(p => p.sources)).toEqual(sources);
    expect(groups.map(p => p.text).join('')).toBe(sources.map(s => s.text).join(''));
    for (const p of groups) {expect(p.start_t).toBe(p.sources[0].start_t); expect(p.end_t).toBe(p.sources.at(-1)!.end_t)}
  });
  it('keeps a two-second pause separate and does not highlight silent gaps', () => {
    const sources = [segment(0, '开头。', 0, 2), segment(1, '后面。', 3, 4), segment(2, '另一段。', 6, 7)];
    const groups = groupTranscript(sources, 480);
    expect(groups).toHaveLength(2);
    expect(isSpeaking(groups[0].sources, 2.5)).toBe(false); expect(isSpeaking(groups[0].sources, 3)).toBe(true);
    expect(isSpeaking(groups[0].sources, 4)).toBe(false);
  });
  it('accepts quoted sentence endings, without treating every sentence as a paragraph', () => {
    const sources = [segment(0, '他说：“你好。”', 0, 12), segment(1, '结束！）', 12, 24), segment(2, '下一段', 24, 25)];
    expect(groupTranscript(sources, 480).map(p => p.sources.length)).toEqual([2, 1]);
  });
  it('handles punctuation-free speech at source boundaries, preserving long indivisible records', () => {
    const sources = Array.from({length: 12}, (_, i) => segment(i, '没有标点', i * 10, i * 10 + 10));
    expect(groupTranscript(sources, 480).every(p => p.end_t - p.start_t <= 44)).toBe(true);
    const long = segment(99, '长'.repeat(400), 0, 120);
    expect(groupTranscript([long], 480)[0].sources).toEqual([long]);
    expect(groupTranscript([long], 480)[0].text).toHaveLength(400);
  });
  it('uses Unicode character budgets and keeps overlap rather than inventing timestamps', () => {
    const sources = [segment(0, '😀'.repeat(160), 0, 10), segment(1, '字'.repeat(161), 8, 12)];
    expect(groupTranscript(sources, 480)).toHaveLength(2);
    const overlap = groupTranscript([segment(0, '一', 0, 10), segment(1, '二', 8, 9)], 480);
    expect(overlap).toHaveLength(1); expect(overlap[0].end_t).toBe(10);
  });
  it('preserves English word separation and all original intra-segment whitespace', () => {
    const sources = [segment(0, 'Hello  world.'), segment(1, 'A tree.'), segment(2, '中文。')];
    expect(groupTranscript(sources, 480)[0].text).toBe('Hello  world. A tree.中文。');
    expect(groupTranscript(sources, 480).flatMap(p => p.sources).map(s => s.text)).toEqual(sources.map(s => s.text));
  });
  it('incremental pages equal full grouping, retaining stable finished groups and extending the tail', () => {
    const sources = Array.from({length: 254}, (_, i) => segment(i));
    const builder = new ParagraphBuilder(800);
    const first = builder.append(sources.slice(0, 100)), saved = structuredClone(first);
    builder.append(sources.slice(100, 200)); const actual = builder.append(sources.slice(200));
    expect(actual).toEqual(groupTranscript(sources, 800));
    expect(first).toEqual(saved);
    expect(actual.slice(0, first.length - 1)).toEqual(first.slice(0, -1));
    expect(actual[first.length - 1].id).toBe(first.at(-1)!.id);
    expect(new Set(actual.flatMap(p => p.sources.map(sourceId))).size).toBe(254);
  });
  it('handles empty and single-segment manuscripts', () => {
    expect(groupTranscript([], 480)).toEqual([]);
    expect(groupTranscript([segment(0)], 480)).toHaveLength(1);
  });
  it('processes the maximum 20000 records incrementally without omissions', () => {
    const sources = Array.from({length: 20000}, (_, i) => segment(i));
    const builder = new ParagraphBuilder(60000);
    for (let i = 0; i < sources.length; i += 100) builder.append(sources.slice(i, i + 100));
    expect(builder.snapshot().flatMap(p => p.sources)).toEqual(sources);
  });
  it('falls back for missing, invalid or blocked preferences', () => {
    expect(readMode({getItem: () => 'segments'})).toBe('segments');
    expect(readMode({getItem: () => 'bad'})).toBe('paragraphs');
    expect(readMode({getItem: () => null})).toBe('paragraphs');
    expect(readMode({getItem: () => {throw Error('blocked')}})).toBe('paragraphs');
  });
});
