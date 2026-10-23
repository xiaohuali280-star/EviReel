import {useCallback, useLayoutEffect, useRef, useState} from 'react';
import type {CSSProperties, KeyboardEvent, PointerEvent} from 'react';
import {DEFAULT_PANEL_RATIO, PANEL_PREFERENCE_KEY, panelBounds, panelPreference, panelRatio} from './panel-layout';

function savedPreference() {
  try {return panelPreference(localStorage.getItem(PANEL_PREFERENCE_KEY))} catch {return DEFAULT_PANEL_RATIO}
}
function savePreference(ratio: number) {
  try {localStorage.setItem(PANEL_PREFERENCE_KEY, String(ratio))} catch { /* resizing still works without storage */ }
}
type Drag = {id: number; element: HTMLDivElement; startX: number; startRatio: number; before: number; next: number; width: number};

export function useResizablePanels() {
  const container = useRef<HTMLDivElement>(null);
  const [preferred, setPreferred] = useState(savedPreference);
  const preference = useRef(preferred);
  const [size, setSize] = useState({width: 0, wide: false});
  const measured = useRef(size);
  const drag = useRef<Drag | null>(null);
  const frame = useRef<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const apply = useCallback((value: number) => {preference.current = value; setPreferred(value)}, []);
  const finish = useCallback((commit: boolean, update = true) => {
    const current = drag.current;
    drag.current = null;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    document.documentElement.classList.remove('is-resizing-panels');
    if (current) {
      if (update) {apply(commit ? current.next : current.before); setDragging(false)}
      if (commit) savePreference(current.next);
      if (current.element.hasPointerCapture(current.id)) current.element.releasePointerCapture(current.id);
    }
  }, [apply]);

  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    const media = matchMedia('(min-width: 901px)');
    const measure = () => {
      const css = getComputedStyle(element);
      const width = Math.max(0, element.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight) - parseFloat(css.getPropertyValue('--split-gap')));
      if (measured.current.width !== width || measured.current.wide !== media.matches) {
        finish(false);
        measured.current = {width, wide: media.matches};
        setSize(measured.current);
      }
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && drag.current) {event.preventDefault(); finish(false)}
    };
    const blur = () => finish(false);
    const observer = new ResizeObserver(measure);
    observer.observe(element); media.addEventListener('change', measure);
    window.addEventListener('keydown', escape); window.addEventListener('blur', blur);
    measure();
    return () => {
      observer.disconnect(); media.removeEventListener('change', measure);
      window.removeEventListener('keydown', escape); window.removeEventListener('blur', blur);
      finish(false, false);
    };
  }, [finish]);

  const bounds = panelBounds(size.width);
  const stacked = !size.wide || !bounds;
  const ratio = panelRatio(preferred, size.width);
  const reset = () => {finish(false); apply(DEFAULT_PANEL_RATIO); savePreference(DEFAULT_PANEL_RATIO)};
  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (stacked || event.button !== 0 || !event.isPrimary || drag.current) return;
    event.preventDefault(); event.currentTarget.focus({preventScroll: true});
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = {id: event.pointerId, element: event.currentTarget, startX: event.clientX,
      startRatio: ratio, before: preference.current, next: ratio, width: size.width};
    document.documentElement.classList.add('is-resizing-panels'); setDragging(true);
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || event.pointerId !== current.id) return;
    current.next = panelRatio(current.startRatio + (event.clientX - current.startX) / current.width, current.width);
    if (frame.current === null) frame.current = requestAnimationFrame(() => {
      frame.current = null;
      if (drag.current) apply(drag.current.next);
    });
  };
  const key = (event: KeyboardEvent<HTMLDivElement>) => {
    if (stacked || !bounds || drag.current) return;
    let next: number;
    if (event.key === 'ArrowLeft') next = ratio - (event.shiftKey ? .05 : .02);
    else if (event.key === 'ArrowRight') next = ratio + (event.shiftKey ? .05 : .02);
    else if (event.key === 'Home') next = bounds.min;
    else if (event.key === 'End') next = bounds.max;
    else if (event.key === 'Enter') {event.preventDefault(); reset(); return}
    else return;
    event.preventDefault(); next = panelRatio(next, size.width); apply(next); savePreference(next);
  };
  const percent = Math.round(ratio * 1000) / 10;
  return {container, stacked, dragging, style: {'--main-size': `${size.width * ratio}px`} as CSSProperties,
    separator: {
      role: 'separator' as const, tabIndex: stacked ? -1 : 0, hidden: stacked,
      'aria-label': '调整学习区与 AI 助手宽度', 'aria-orientation': 'vertical' as const,
      'aria-controls': 'learning-panel assistant-panel', 'aria-valuemin': Math.round((bounds?.min ?? .4) * 1000) / 10,
      'aria-valuemax': Math.round((bounds?.max ?? .75) * 1000) / 10, 'aria-valuenow': percent,
      'aria-valuetext': `学习区 ${percent}%，AI 助手 ${Math.round((100 - percent) * 10) / 10}%`,
      title: '左右拖动调整宽度，双击恢复默认', onPointerDown: down, onPointerMove: move,
      onPointerUp: (event: PointerEvent<HTMLDivElement>) => {if (drag.current?.id === event.pointerId) finish(true)},
      onPointerCancel: (event: PointerEvent<HTMLDivElement>) => {if (drag.current?.id === event.pointerId) finish(false)},
      onLostPointerCapture: (event: PointerEvent<HTMLDivElement>) => {if (drag.current?.id === event.pointerId) finish(false)},
      onDoubleClick: reset, onKeyDown: key,
    }};
}
