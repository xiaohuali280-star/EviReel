import {useCallback, useEffect, useRef, useState} from 'react';
import {api, errorText} from './api';
import {ParagraphBuilder, targetDuration} from './transcript-reading';
import type {Paragraph} from './transcript-reading';
import type {Page, Transcript} from './types';

interface Cache {items: Transcript[]; paragraphs: Paragraph[]; total: number; loaded: boolean; version: string | null}
const empty = (): Cache => ({items: [], paragraphs: [], total: 0, loaded: false, version: null});
export function useTranscript(videoId: string, version: string | null, duration: number, enabled: boolean) {
  const [cache, setCache] = useState<Cache>(empty);
  const state = useRef(cache);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const builder = useRef(new ParagraphBuilder(duration));
  const durationRef = useRef(duration); durationRef.current = duration;
  const target = targetDuration(duration);
  // Request ownership is invalidated on every text-version change and unmount.
  useEffect(() => {
    epoch.current++; request.current?.abort(); request.current = null;
    state.current = empty(); setCache(state.current); setLoading(false); setError('');
    builder.current = new ParagraphBuilder(durationRef.current);
    return () => {epoch.current++; request.current?.abort(); request.current = null};
  }, [videoId, version]);
  useEffect(() => {
    builder.current = new ParagraphBuilder(durationRef.current);
    const next = {...state.current, paragraphs: builder.current.append(state.current.items)};
    state.current = next; setCache(next);
  }, [target]);
  const load = useCallback(async () => {
    if (request.current) return;
    const controller = new AbortController(), generation = epoch.current;
    request.current = controller; setLoading(true); setError('');
    const owns = () => generation === epoch.current && request.current === controller && !controller.signal.aborted;
    try {
      const previous = state.current;
      let offset = previous.items.length;
      let page = await api<Page<Transcript>>(`/api/videos/${videoId}/transcripts?kind=segments&limit=100&offset=${offset}`, {signal: controller.signal});
      if (!owns()) return;
      if (offset && (!page.items.length || page.total < offset || page.items[0].transcript_version !== previous.version)) {
        offset = 0;
        page = await api<Page<Transcript>>(`/api/videos/${videoId}/transcripts?kind=segments&limit=100&offset=0`, {signal: controller.signal});
        if (!owns()) return;
      }
      if (page.total < offset || (!page.items.length && page.total > offset) || page.items.some(s => s.transcript_version !== page.items[0].transcript_version)) {
        throw new Error('inconsistent transcript page');
      }
      if (!offset) builder.current = new ParagraphBuilder(durationRef.current);
      const items = offset ? [...previous.items, ...page.items] : page.items;
      const next = {items, paragraphs: builder.current.append(page.items), total: page.total, loaded: true,
        version: page.items[0]?.transcript_version ?? previous.version};
      state.current = next; setCache(next);
    } catch (e) {if (owns()) setError(errorText(e))}
    finally {if (owns()) {request.current = null; setLoading(false)}}
  }, [videoId]);
  useEffect(() => {if (enabled && !state.current.loaded && !error) void load()}, [enabled, version, load, error]);
  return {...cache, loading, error, load};
}
