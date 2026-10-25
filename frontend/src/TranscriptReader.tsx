import {useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react';
import {FileText} from 'lucide-react';
import {time} from './api';
import {isSpeaking, modeKey, readMode, sourceId} from './transcript-reading';
import type {ReadingMode} from './transcript-reading';
import {useTranscript} from './use-transcript';
import type {Transcript, Video} from './types';

export function TranscriptReader({video, visible, playbackTime, downloading, download, retry, seek, onSegments}: {
  video: Video; visible: boolean; playbackTime: number; downloading: boolean;
  download: (url: string) => void; retry: () => void; seek: (t: number) => void;
  onSegments: (items: Transcript[]) => void;
}) {
  const [mode, setMode] = useState<ReadingMode>(() => {try {return readMode()} catch {return 'paragraphs'}});
  const data = useTranscript(video.video_id, video.transcript.text_version, video.duration, visible && video.capabilities.transcript);
  useEffect(() => {onSegments(data.items)}, [data.items, onSegments]);
  const body = useRef<HTMLDivElement>(null);
  const anchor = useRef<{id: string; offset: number} | null>(null);
  const rows = useMemo(() => mode === 'paragraphs' ? data.paragraphs : data.items.map(s => ({id: sourceId(s), text: s.text, start_t: s.start_t, end_t: s.end_t, sources: [s]})), [mode, data.paragraphs, data.items]);
  function capture() {
    const container = body.current;
    if (!container) return;
    const bounds = container.getBoundingClientRect();
    const node = Array.from(container.querySelectorAll<HTMLElement>('[data-source]')).find(n => n.getBoundingClientRect().bottom > bounds.top);
    if (node) anchor.current = {id: node.dataset.source!, offset: node.getBoundingClientRect().top - bounds.top};
    else {
      const row = rows.find(r => isSpeaking(r.sources, playbackTime)) || rows[0];
      if (row) anchor.current = {id: sourceId(row.sources[0]), offset: 0};
    }
  }
  useLayoutEffect(() => {
    if (!visible || !body.current || !anchor.current) return;
    const saved = anchor.current, index = rows.findIndex(r => r.sources.some(s => sourceId(s) === saved.id));
    const node = body.current.querySelectorAll<HTMLElement>('[data-source]')[index];
    if (node) {
      const bounds = node.getBoundingClientRect();
      const offset = Math.max(saved.offset, -Math.max(0, bounds.height - 20));
      body.current.scrollTop += bounds.top - body.current.getBoundingClientRect().top - offset;
    }
    anchor.current = null;
  }, [rows, visible]);
  const rememberedScroll = useRef(0);
  useLayoutEffect(() => {if (visible && body.current) body.current.scrollTop = rememberedScroll.current}, [visible]);
  function changeMode(next: ReadingMode) {
    if (next === mode) return;
    capture(); setMode(next);
    try {localStorage.setItem(modeKey, next)} catch { /* optional preference */ }
  }
  const exportURL = (format: string) => `/api/videos/${video.video_id}/transcripts/export?format=${format}`;
  return <div className="manuscript" hidden={!visible}>
    <div className="artifact-tools"><span>语音识别文稿 · 未经人工校对</span>{video.capabilities.transcript && <>{['txt', 'srt'].map(format => <a key={format} href={exportURL(format)} aria-disabled={downloading} onClick={e => {e.preventDefault(); download(exportURL(format))}}>{format.toUpperCase()}</a>)}</>}<button onClick={retry}>重试语音处理</button></div>
    {!video.capabilities.transcript ? <div className="content-empty"><FileText size={35} /><h3>文稿暂不可用</h3><p>{video.transcript.text_status === 'empty' ? '该视频未识别到语音内容。' : '语音处理尚未完成或失败，可重试。'}</p></div> : <>
      <div className="reading-toolbar"><div role="group" aria-label="文稿阅读模式">{([['paragraphs', '段落阅读'], ['segments', '逐句模式']] as const).map(([value, label]) => <button key={value} aria-pressed={mode === value} className={mode === value ? 'selected' : ''} onClick={() => changeMode(value)}>{label}</button>)}</div><span>{data.loaded ? `已加载 ${data.items.length}/${data.total} 条原始片段 · ${rows.length}${mode === 'paragraphs' ? ' 段' : ' 条'}` : '正在读取文稿…'}</span></div>
      <p className="reading-hint">{mode === 'paragraphs' ? '按句子与停顿自动合并，原文完整保留。点击段落跳转至段首。' : '显示原始识别片段，便于逐条核对和精确定位。'}</p>
      <div ref={body} className="transcript-reader" aria-label="文稿正文" tabIndex={0} onScroll={e => {if (visible) {rememberedScroll.current = e.currentTarget.scrollTop; capture()}}}>
        {rows.map(r => <button key={r.id} data-source={sourceId(r.sources[0])} className={`transcript-line ${mode === 'paragraphs' ? 'transcript-paragraph' : ''} ${isSpeaking(r.sources, playbackTime) ? 'current' : ''}`} disabled={!video.capabilities.playback} onClick={() => seek(r.sources[0].start_t)}><time>{time(r.start_t)}{mode === 'paragraphs' && `–${time(r.end_t)}`}</time><span>{r.text}</span></button>)}
        {data.loaded && !data.items.length && <p className="content-empty">该视频未识别到语音内容。</p>}
      </div>
      {data.loading && <p role="status" className="loading">正在读取文稿…</p>}
      {data.error && <div role="alert" className="status-banner"><span>{data.error}</span><button onClick={() => {capture(); void data.load()}}>重试读取文稿</button></div>}
      {data.items.length < data.total && <button className="secondary load-more" disabled={data.loading} onClick={() => {capture(); void data.load()}}>加载更多（{data.items.length}/{data.total}）</button>}
    </>}
  </div>;
}
