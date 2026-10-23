import {useEffect, useState} from 'react';
import {api, codeText, errorText, time} from './api';
import type {ChatAction, ChatResource, ResourceState} from './types';

export const actionMessages: Record<ChatAction, string> = {
  notes: '请根据当前视频生成完整的章节与学习笔记，覆盖主要内容，并提供来源时间定位。',
  video: '请帮我理解当前视频的主要内容、核心观点和关键时间点；结合当前可用的画面与文稿，并说明依据。',
  transcript: '请提取当前视频的完整语音文稿，保留时间标记，并提供 TXT 和 SRT 下载。',
  slides: '请根据当前视频整理学习课件，包含章节标题、核心知识点和来源时间，并提供 PPTX 和 PDF 下载。',
};

export function ResourceCard({resource, videoId, download, view, seek, refreshed}: {
  resource: ChatResource; videoId: string; download: (url: string) => void;
  view: (kind: string) => void; seek: (time: number) => void; refreshed: () => void;
}) {
  const [state, setState] = useState<ResourceState | null>(null);
  const [error, setError] = useState('');
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const query = new URLSearchParams({owner: resource.video_id, kind: resource.kind,
    ...(resource.artifact_id ? {artifact_id: resource.artifact_id} : {}),
    ...(resource.job_id ? {job_id: resource.job_id} : {}), ...(resource.version ? {version: resource.version} : {})}).toString();
  useEffect(() => {
    if (resource.video_id !== videoId) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await api<ResourceState>(`/api/videos/${videoId}/chat/resource?${query}&offset=${offset}`, {signal: abort.signal});
        if (abort.signal.aborted) return;
        setState(previous => ({...next, preview: offset > 0 ? [...(previous?.preview || []), ...(next.preview || [])] : next.preview}));
        setError('');
        if (['queued', 'running'].includes(next.status)) timer = setTimeout(load, 1500);
        else if (next.status === 'ready' && offset === 0) refreshed();
      } catch (e) {if (!abort.signal.aborted) setError(errorText(e))}
    }; load();
    return () => {abort.abort(); clearTimeout(timer)};
  }, [videoId, resource.video_id, query, offset, retry, refreshed]);
  if (resource.video_id !== videoId) return null;
  const ready = state?.status === 'ready';
  return <section className="resource-card" aria-label={resource.label || '学习资料'}>
    <strong>{resource.label || '学习资料'}</strong>
    <p role="status">{error || (!state ? '正在读取任务…' : ready ? (state.stale ? '历史成品 · 来源已更新' : '已完成，可查看或下载') : ['queued', 'running'].includes(state.status) ? `后台生成中${state.total ? ` · ${state.current || 0}/${state.total} 批` : ''}` : codeText(state.error_code))}</p>
    {error && <button onClick={() => {setOffset(0); setRetry(n => n + 1)}}>重试读取</button>}
    {ready && <><div className="resource-preview">{state.preview?.map((p, i) => <div key={i}><button disabled={state.stale} onClick={() => seek(p.start_t)}>{time(p.start_t)}</button><span>{p.text}</span></div>)}</div>
      {(state.preview?.length || 0) < (state.total || 0) && <button onClick={() => setOffset(state.preview?.length || 0)}>加载更多</button>}
      <div className="resource-links"><button disabled={state.stale} onClick={() => view(resource.kind)}>在学习区查看</button>{state.links.map(link => <button key={link.url} onClick={() => download(link.url)}>{link.label}</button>)}</div></>}
  </section>;
}
