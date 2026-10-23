export const DEFAULT_PANEL_RATIO = .65;
export const PANEL_PREFERENCE_KEY = 'evireel:panel-ratio';

export function panelPreference(raw: string | null) {
  const value = raw?.trim() ? Number(raw) : NaN;
  return Number.isFinite(value) && value >= .4 && value <= .75 ? value : DEFAULT_PANEL_RATIO;
}

export function panelBounds(width: number) {
  if (!Number.isFinite(width) || width < 740) return null;
  return {min: Math.max(.4, 420 / width), max: Math.min(.75, 1 - 320 / width)};
}

export function panelRatio(preferred: number, width: number) {
  const bounds = panelBounds(width);
  const value = Number.isFinite(preferred) ? preferred : DEFAULT_PANEL_RATIO;
  return bounds ? Math.max(bounds.min, Math.min(bounds.max, value)) : DEFAULT_PANEL_RATIO;
}
