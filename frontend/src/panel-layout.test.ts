import {describe, expect, it} from 'vitest';
import {DEFAULT_PANEL_RATIO, panelBounds, panelPreference, panelRatio} from './panel-layout';

describe('usable panel proportions', () => {
  it('rejects damaged and non-finite stored preferences', () => {
    for (const raw of [null, '', ' ', 'NaN', 'Infinity', '{}', '.1', '.9']) expect(panelPreference(raw)).toBe(DEFAULT_PANEL_RATIO);
    expect(panelPreference('0.55')).toBe(.55);
  });
  it('requires enough real content width for both panel minimums', () => {
    for (const width of [0, -1, 739, NaN, Infinity]) expect(panelBounds(width)).toBe(null);
    expect(panelBounds(740)).toEqual({min: 420 / 740, max: 1 - 320 / 740});
  });
  it('intersects percentage and pixel limits without overflow', () => {
    for (const width of [740, 800, 1000, 1600, 2400]) {
      for (const preferred of [-100, .4, .65, .75, 100, NaN]) {
        const ratio = panelRatio(preferred, width);
        expect(ratio * width).toBeGreaterThanOrEqual(420 - .000001);
        expect((1 - ratio) * width).toBeGreaterThanOrEqual(320 - .000001);
        expect(ratio).toBeGreaterThanOrEqual(.4);
        expect(ratio).toBeLessThanOrEqual(.75);
      }
    }
  });
  it('can restore a preferred proportion after temporary viewport constraints', () => {
    const preferred = .75;
    expect(panelRatio(preferred, 800)).toBe(.6);
    expect(panelRatio(preferred, 1600)).toBe(preferred);
  });
});
