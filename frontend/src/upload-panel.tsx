import {Check, LoaderCircle, Pause, Play, RotateCcw, Video as VideoIcon, X} from 'lucide-react';
import type {UploadSession, UploadState} from './resumable-upload';

const mb = (n: number) => (n / 1024 / 1024).toFixed(n >= 100 * 1024 * 1024 ? 0 : 1);

const failures: Record<string, string> = {
  file_mismatch: '所选文件与未完成的上传不是同一个文件。请选择原文件，或放弃该上传后重新开始。',
  quota_exceeded: '学习空间的存储配额不足，请删除部分视频后重试。',
  too_many_uploads: '未完成的上传过多，请先完成或放弃其中一个。',
  upload_expired: '上传会话已过期，请重新上传。',
  invalid_media: '服务器校验发现文件不是可读的视频，请检查文件后重新上传。',
  no_video_track: '文件中没有视频画面，请选择其他文件。',
  duration_limit: '视频时长超过服务器限制。',
  size_mismatch: '服务器收到的文件大小与原文件不一致，请重新上传。',
  integrity_mismatch: '服务器校验发现内容与上传分片不一致，请重新上传。',
  signature_rejected: '存储服务多次拒绝上传签名，请稍后继续。',
  part_failed: '网络不稳定，部分分片多次失败。可以点击“继续上传”只补传缺失部分。',
  network: '网络连接中断。可以点击“继续上传”只补传缺失部分。',
};
export const uploadFailureText = (code?: string, fallback?: string) => failures[code || ''] || fallback || '上传未完成，可重试。';

function Steps({phase}: {phase: UploadState['phase']}) {
  const index = ['completing', 'verifying'].includes(phase) ? 1 : phase === 'completed' ? 2 : 0;
  return <ol className="upload-steps" aria-label="上传阶段">{['上传分片', '服务器校验', '视频处理'].map((label, i) =>
    <li key={label} data-state={i < index ? 'done' : i === index ? 'active' : 'todo'}>{i < index ? <Check size={13} /> : <span>{i + 1}</span>}{label}</li>)}</ol>;
}

export function UploadPanel({name, state, pause, resume, cancel}: {name: string; state: UploadState; pause: () => void; resume: () => void; cancel: () => void}) {
  const percent = state.totalBytes ? Math.floor(state.sentBytes / state.totalBytes * 100) : 0;
  const verifying = ['completing', 'verifying'].includes(state.phase);
  return <section className="empty-state processing upload-panel" aria-live="polite">
    <div className="empty-icon"><VideoIcon size={70} /></div>
    <span className="eyebrow">{verifying ? '服务器校验中' : state.phase === 'failed' ? '上传暂停' : state.phase === 'cancel_unconfirmed' ? '取消未确认' : '断点续传上传'}</span>
    <h1>把知识带进你的学习空间</h1>
    <p className="filename">{name}</p>
    <Steps phase={state.phase} />
    {state.phase === 'hashing' && <p><LoaderCircle className="spin" size={16} /> 正在读取文件，检查可续传的分片…</p>}
    {!verifying && state.phase !== 'hashing' && <>
      <div className="progress" role="progressbar" aria-label="上传进度" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}><span style={{width: `${percent}%`}} /></div>
      <strong data-testid="upload-percent">{percent}%</strong>
      <p data-testid="upload-detail">{mb(state.sentBytes)} / {mb(state.totalBytes)} MB · 分片 {state.partsDone}/{state.partCount || '—'}{state.retries ? ` · 已重试 ${state.retries} 次` : ''}{state.resigned ? ` · 已重新签名 ${state.resigned} 次` : ''}</p>
    </>}
    {verifying && <><div className="indeterminate"><span /></div><p>上传完成。服务器正在核对文件大小、完整性并检查视频格式，通过后才会开始处理。</p></>}
    {state.phase === 'failed' && <p className="upload-error" role="alert">{uploadFailureText(state.errorCode, state.error)}</p>}
    {state.phase === 'cancel_unconfirmed' && <p className="upload-error" role="alert" data-testid="cancel-unconfirmed">取消请求没有得到服务器确认（网络可能中断）。上传已在本机停止，但服务器上的会话可能仍然保留并占用配额。请重试取消，或继续上传。</p>}
    <div className="upload-actions">
      {state.phase === 'uploading' && <button className="secondary" onClick={pause}><Pause size={16} />暂停</button>}
      {(state.phase === 'paused' || state.phase === 'cancel_unconfirmed' || (state.phase === 'failed' && !['file_mismatch', 'quota_exceeded', 'upload_expired', 'too_many_uploads'].includes(state.errorCode || '') && !state.errorCode?.startsWith('upload_')))
        && <button className="primary" onClick={resume}><Play size={16} />继续上传</button>}
      {!verifying && state.phase !== 'completed' && <button className="secondary" onClick={cancel}><X size={16} />{state.phase === 'failed' ? '关闭' : state.phase === 'cancel_unconfirmed' ? '重试取消' : '取消上传'}</button>}
    </div>
    {!verifying && <small>可以暂停或刷新页面；重新选择同一文件后只会补传缺失的分片。</small>}
  </section>;
}

export function ResumeBanner({sessions, choose, discard}: {sessions: UploadSession[]; choose: (s: UploadSession) => void; discard: (s: UploadSession) => void}) {
  if (!sessions.length) return null;
  return <div className="resume-banner" role="region" aria-label="未完成的上传">{sessions.map(s => {
    const percent = Math.floor(s.uploaded_bytes / s.declared_size * 100);
    const checking = ['completing', 'verifying'].includes(s.state);
    return <div key={s.upload_id} className="resume-item">
      <RotateCcw size={16} />
      <span><strong>{s.original_name}</strong><small>{checking ? '上传已完成，服务器正在校验…' : `已上传 ${percent}% · 浏览器重开后需要重新选择同一文件，系统会校验文件身份后只补传缺失分片`}</small></span>
      {!checking && <><button className="primary" onClick={() => choose(s)}>选择文件继续</button><button className="secondary" onClick={() => discard(s)}>放弃</button></>}
    </div>;
  })}</div>;
}
