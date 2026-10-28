export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {super(message)}
}
export async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event('evireel:unauthenticated'));
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
  chat_source_not_ready: '视频内容尚未准备好，请先生成文稿或完成视频处理。',
  chat_context_stale: '所选章节或文稿已更新，请重新选择后发送。',
  chat_context_limit: '当前材料超出问答范围，请选择章节或先生成完整笔记。',
  chat_invalid_output: '回答未通过来源校验，请重试。',
  chat_model_auth_failed: '模型密钥无效或没有调用权限，请检查服务器配置并重启后端。',
  chat_model_unavailable: '模型暂不可用，请检查模型名称或稍后重试。',
  chat_model_rate_limited: '模型服务额度不足或请求过于频繁，请检查余额和限额。',
  chat_model_request_rejected: '模型接口拒绝了请求，请检查模型与接口是否兼容。',
  chat_model_connection_failed: '与模型服务的连接中断，请稍后重试。',
  chat_model_incomplete: '模型回答未完整返回，请重新发送。',
  asr_disabled: '服务器尚未启用语音识别，请启用后生成文稿。',
  transcript_empty: '该视频没有识别到可用语音。', transcript_not_ready: '文稿尚未就绪，请重试语音处理。',
  conversation_busy: '当前对话正在处理，请等待完成。', conversation_capacity: '回答任务较多，请稍后重试。',
  video_busy: '视频正在使用中，请等待当前操作完成后重试。', index_running: '视频正在处理中，请稍后重试。',
  model_not_configured: '请在服务器配置模型服务后使用 AI 功能。', study_generation_failed: '笔记生成失败，旧成品已保留，可重新生成。',
  study_budget_exceeded: '视频超过当前完整笔记的生成预算，请调整服务器限额后重试。', study_timeout: '笔记生成超时，可重新生成。',
  study_model_auth_failed: '模型密钥无效或没有调用权限，请检查服务器配置，修改后重启后端。',
  study_model_unavailable: '模型暂不可用或模型名称不正确，请检查配置或稍后重试。',
  study_model_rate_limited: '模型服务额度不足或请求过于频繁，请检查余额和限额后重试。',
  study_model_request_rejected: '模型接口拒绝了请求，请检查模型与接口是否兼容。',
  study_model_connection_failed: '与模型服务的连接中断，请检查网络或代理后重新生成。',
  study_model_incomplete: '模型响应未完整生成，请重新生成；较长视频可调小服务器的笔记分批大小。',
  study_invalid_output: '模型笔记格式或来源引用校验未通过，请重新生成。',
  study_source_empty: '没有可用的语音或画面文字，请重试视频处理。', study_source_not_ready: '视频内容尚未准备好，请先完成处理。',
  server_restart: '服务重启中断了任务，请重新发起。', server_shutdown: '服务停止中断了任务，请重新发起。',
  agent_failed: '回答生成失败，请稍后重试。', index_not_ready: '视频检索尚未就绪，请重试处理。',
  video_trashed: '视频已移入回收站，恢复后即可继续学习。', request_timeout: '回答超时，请重新提问。',
  queue_full: '处理队列已满，请稍后重试。', study_queue_full: '笔记生成任务较多，请稍后重试。',
  study_running: '该视频正在生成笔记，请等待完成。', question_too_large: '问题超过字数上限，请缩短后重试。',
  invalid_credentials: '用户名或密码不正确。', unauthenticated: '登录已过期，请重新登录。',
  quota_exceeded: '学习空间的存储配额不足，请删除部分视频后重试。', too_many_uploads: '未完成的上传过多，请先完成或放弃其中一个。',
  upload_not_found: '上传会话不存在或无权访问。', video_not_found: '视频不存在或无权访问。',
  storage_unavailable: '对象存储暂不可用，请稍后重试。', upload_limit: '视频超过上传大小上限。', unsupported_media: '不支持的视频格式。',
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
