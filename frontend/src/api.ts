export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {super(message)}
}
export async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.error?.code || 'http_error', body.error?.message || `请求失败（${response.status}）`, response.status);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const post = <T>(url: string, body?: unknown) => api<T>(url, {method: 'POST', body: body === undefined ? undefined : JSON.stringify(body)});
export const requestId = () => 'r_' + crypto.randomUUID().replaceAll('-', '');
export const isProcessing = (status: string) => ['queued', 'running'].includes(status);
export function time(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '00:00';
  const value = Math.floor(seconds);
  return (value >= 3600 ? `${Math.floor(value / 3600)}:` : '') + `${Math.floor(value / 60) % 60}`.padStart(2, '0') + ':' + `${value % 60}`.padStart(2, '0');
}
export function seekTime(target: number, duration: number) {
  return Number.isFinite(target) && Number.isFinite(duration) && duration > 0 ? Math.min(Math.max(0, target), Math.max(0, duration - .05)) : null;
}
const messages: Record<string, string> = {
  video_busy: '视频正在使用中，请等待当前操作完成后重试。', index_running: '视频正在处理中，请稍后重试。',
  model_not_configured: '请在服务器配置模型服务后再生成笔记。', study_generation_failed: '笔记生成失败，旧成品已保留，可重新生成。',
  study_budget_exceeded: '视频超过当前完整笔记的生成预算，请调整服务器限额后重试。', study_timeout: '笔记生成超时，可重新生成。',
  study_source_empty: '没有可用的语音或画面文字，请重试视频处理。', study_source_not_ready: '视频内容尚未准备好，请先完成处理。',
  server_restart: '服务重启中断了任务，请重新发起。', server_shutdown: '服务停止中断了任务，请重新发起。',
  agent_failed: '回答生成失败，请稍后重试。', index_not_ready: '视频检索尚未就绪，请重试处理。',
  video_trashed: '视频已移入回收站，恢复后即可继续学习。', request_timeout: '回答超时，请重新提问。',
  queue_full: '处理队列已满，请稍后重试。', study_queue_full: '笔记生成任务较多，请稍后重试。',
  study_running: '该视频正在生成笔记，请等待完成。', question_too_large: '问题超过字数上限，请缩短后重试。',
  export_budget_exceeded: '课件超过当前导出页数上限，可下载笔记或调整服务器限额后重试。',
};
export function errorText(error: unknown) {
  if (error instanceof ApiError) return messages[error.code] || `${error.message}（${error.code}）`;
  return error instanceof Error && error.name === 'AbortError' ? '' : '连接失败，请检查网络后重试。';
}
export const codeText = (code?: string) => messages[code || ''] || `操作未完成${code ? `（${code}）` : ''}，可重试。`;
export async function downloadFile(url: string) {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.error?.code || 'download_failed', body.error?.message || '下载失败', response.status);
  }
  const blob = await response.blob();
  const objectURL = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectURL;
  link.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'EviReel-export';
  link.click();
  setTimeout(() => URL.revokeObjectURL(objectURL), 60000);
}
export function upload(file: File, progress: (value: number) => void, signal: AbortSignal) {
  return new Promise<{video_id: string; job_id: string}>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    signal.addEventListener('abort', abort, {once: true});
    xhr.open('POST', '/api/videos');
    xhr.upload.onprogress = e => {if (e.lengthComputable) progress(Math.round(e.loaded / e.total * 100))};
    xhr.onloadend = () => signal.removeEventListener('abort', abort);
    xhr.onload = () => {
      let body;
      try {body = JSON.parse(xhr.responseText)} catch {reject(new ApiError('invalid_response', '服务器响应无效', xhr.status)); return}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(body.error?.code || 'upload_failed', body.error?.message || '上传失败', xhr.status));
    };
    xhr.onerror = () => reject(new Error('network'));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    if (signal.aborted) {reject(new DOMException('Upload cancelled', 'AbortError')); return}
    const data = new FormData(); data.append('video', file); xhr.send(data);
  });
}
