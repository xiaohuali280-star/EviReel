// Resumable, concurrent, direct-to-storage upload client (E1).
// The server's ListParts-backed progress is authoritative; this client only decides
// which parts to (re)send. Storage I/O, signing and hashing are injectable for tests.
import {md5, toBase64} from './md5';

export interface UploadSession {
  upload_id: string; video_id: string; state: string; original_name: string; declared_size: number;
  part_size: number; part_count: number; client_fingerprint: string; uploaded_parts: number[];
  mismatched_parts: number[]; uploaded_bytes: number; missing_parts: number[]; expires_at: string;
  error: {code: string; message: string} | null; result: {video_id: string; index_error?: {code: string}} | null; job_id: string | null;
}
export type Phase = 'hashing' | 'uploading' | 'paused' | 'completing' | 'verifying' | 'completed' | 'failed' | 'cancelled';
export interface UploadState {
  phase: Phase; session?: UploadSession; sentBytes: number; totalBytes: number;
  partsDone: number; partCount: number; retries: number; resigned: number; error?: string; errorCode?: string;
}
export interface PutResult {status: number; body?: string}
export interface Transport {
  json<T>(url: string, init?: {method?: string; body?: unknown}): Promise<T>;
  put(url: string, body: Blob, md5: string, onProgress: (loaded: number) => void, signal: AbortSignal): Promise<PutResult>;
  md5(blob: Blob): Promise<string>;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
}
export class UploadError extends Error {constructor(public code: string, message: string) {super(message)}}

// Identity of a *local* file for resume prompts: size + sampled SHA-256. Not a security hash;
// the server recomputes the full SHA-256 of the assembled object during verification.
export async function fingerprint(file: Blob): Promise<string> {
  const sample = 1 << 20, size = file.size;
  const ranges: [number, number][] = [[0, Math.min(sample, size)]];
  for (const at of [0.25, 0.5, 0.75]) {const start = Math.floor(size * at); ranges.push([start, Math.min(size, start + 65536)])}
  if (size > sample) ranges.push([size - sample, size]);
  const parts = await Promise.all(ranges.map(([a, b]) => file.slice(a, b).arrayBuffer()));
  const header = new TextEncoder().encode(`evireel-fp-v1:${size}:`);
  const all = new Uint8Array(header.length + parts.reduce((n, p) => n + p.byteLength, 0));
  all.set(header); let offset = header.length;
  for (const p of parts) {all.set(new Uint8Array(p), offset); offset += p.byteLength}
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', all));
  return 'fp1-' + Array.from(digest, b => b.toString(16).padStart(2, '0')).join('');
}

export function partRange(session: Pick<UploadSession, 'declared_size' | 'part_size' | 'part_count'>, n: number): [number, number] {
  const start = (n - 1) * session.part_size;
  return [start, n === session.part_count ? session.declared_size : start + session.part_size];
}

interface Signed {url: string; expiresAt: number}

export class ResumableUpload {
  state: UploadState;
  private abort = new AbortController();
  private signed = new Map<number, Signed>();
  private inflight = new Map<number, number>();
  private done = new Set<number>();
  private queue: number[] = [];
  private session?: UploadSession;
  private finished?: Promise<UploadSession>;

  constructor(private file: File, private transport: Transport, private onState: (s: UploadState) => void,
              private options = {concurrency: 3, maxRetries: 6, signBatch: 6, pollMs: 1500, now: () => Date.now()}) {
    this.state = {phase: 'hashing', sentBytes: 0, totalBytes: file.size, partsDone: 0, partCount: 0, retries: 0, resigned: 0};
  }

  private emit(patch: Partial<UploadState>) {
    const inflight = [...this.inflight.values()].reduce((a, b) => a + b, 0);
    const doneBytes = this.session ? [...this.done].reduce((n, p) => n + partRange(this.session!, p)[1] - partRange(this.session!, p)[0], 0) : 0;
    this.state = {...this.state, ...patch, sentBytes: doneBytes + inflight, partsDone: this.done.size,
      partCount: this.session?.part_count || 0, session: this.session};
    this.onState(this.state);
  }

  /** Start a new session, or resume `existing` after checking that the reselected file is the same file. */
  start(existing?: UploadSession): Promise<UploadSession> {
    this.finished = this.run(existing);
    return this.finished;
  }

  pause() {
    if (this.state.phase !== 'uploading') return;
    this.abort.abort();
    this.emit({phase: 'paused'});
  }

  resume(): Promise<UploadSession> {
    if (this.state.phase !== 'paused' && this.state.phase !== 'failed') return this.finished!;
    this.abort = new AbortController();
    return this.start(this.session);
  }

  async cancel() {
    this.abort.abort();
    if (this.session) await this.transport.json(`/api/uploads/${this.session.upload_id}`, {method: 'DELETE'}).catch(() => undefined);
    this.emit({phase: 'cancelled'});
  }

  private async run(existing?: UploadSession): Promise<UploadSession> {
    const signal = this.abort.signal;
    try {
      this.emit({phase: 'hashing', error: undefined, errorCode: undefined});
      const fp = await fingerprint(this.file);
      if (existing) {
        if (existing.declared_size !== this.file.size || existing.client_fingerprint !== fp)
          throw new UploadError('file_mismatch', '所选文件与未完成的上传不是同一个文件，请选择原文件或放弃该上传。');
        this.session = await this.transport.json<UploadSession>(`/api/uploads/${existing.upload_id}`);
      } else {
        this.session = await this.transport.json<UploadSession>('/api/uploads', {method: 'POST',
          body: {filename: this.file.name, size: this.file.size, fingerprint: fp, content_type: this.file.type}});
      }
      remember(this.session);
      if (['completing', 'verifying', 'completed'].includes(this.session.state)) return await this.waitVerified(signal);
      if (this.session.state !== 'uploading') throw new UploadError(this.session.error?.code || `upload_${this.session.state}`, '上传会话已结束，请重新上传。');
      for (;;) {
        this.done = new Set(this.session.uploaded_parts);
        this.queue = this.session.missing_parts.filter(n => !this.done.has(n));
        this.emit({phase: 'uploading'});
        // One failing part stops its siblings; a user pause/cancel (outer signal) stops all.
        const round = new AbortController();
        const stop = () => round.abort();
        signal.addEventListener('abort', stop, {once: true});
        try {
          await Promise.all(Array.from({length: Math.min(this.options.concurrency, Math.max(1, this.queue.length))}, () => this.worker(round.signal)));
        } catch (e) {round.abort(); throw e}
        finally {signal.removeEventListener('abort', stop)}
        if (signal.aborted) return this.session!;
        // Re-check against storage before completing: a "done" locally is only a hint.
        this.session = await this.transport.json<UploadSession>(`/api/uploads/${this.session.upload_id}`);
        if (this.session.missing_parts.length) continue;
        this.emit({phase: 'completing'});
        try {
          this.session = await this.transport.json<UploadSession>(`/api/uploads/${this.session.upload_id}/complete`, {method: 'POST'});
        } catch (e) {
          if (e instanceof UploadError && ['parts_missing', 'parts_invalid'].includes(e.code)) {
            this.session = await this.transport.json<UploadSession>(`/api/uploads/${this.session.upload_id}`); continue;
          }
          throw e;
        }
        return await this.waitVerified(signal);
      }
    } catch (e) {
      if (signal.aborted) return this.session!;
      const code = e instanceof UploadError ? e.code : 'network';
      this.emit({phase: 'failed', errorCode: code, error: e instanceof Error ? e.message : String(e)});
      throw e;
    }
  }

  private async waitVerified(signal: AbortSignal): Promise<UploadSession> {
    for (;;) {
      if (this.session!.state === 'completed') {forget(this.session!.upload_id); this.emit({phase: 'completed'}); return this.session!}
      if (['failed', 'aborted', 'expired'].includes(this.session!.state)) {
        forget(this.session!.upload_id);
        throw new UploadError(this.session!.error?.code || this.session!.state, this.session!.error?.message || '上传未完成');
      }
      this.emit({phase: this.session!.state === 'completing' ? 'completing' : 'verifying'});
      await this.transport.sleep(this.options.pollMs, signal);
      if (signal.aborted) return this.session!;
      this.session = await this.transport.json<UploadSession>(`/api/uploads/${this.session!.upload_id}`);
    }
  }

  private async worker(signal: AbortSignal) {
    while (!signal.aborted) {
      const part = this.queue.shift();
      if (part === undefined) return;
      try {await this.sendPart(part, signal)}
      catch (e) {if (!signal.aborted) this.queue.unshift(part); throw e}
    }
  }

  private async sign(parts: number[], md5s: Map<number, string>) {
    const s = this.session!;
    const body = {parts: parts.map(n => {const [a, b] = partRange(s, n); return {part_number: n, size: b - a, md5_b64: md5s.get(n)!}})};
    const result = await this.transport.json<{expires_in: number; parts: {part_number: number; url: string}[]}>(
      `/api/uploads/${s.upload_id}/parts/sign`, {method: 'POST', body});
    const expiresAt = this.options.now() + result.expires_in * 1000 - Math.min(5000, result.expires_in * 250);
    for (const p of result.parts) this.signed.set(p.part_number, {url: p.url, expiresAt});
  }

  private md5s = new Map<number, string>();
  private async digest(n: number) {
    if (!this.md5s.has(n)) {const [a, b] = partRange(this.session!, n); this.md5s.set(n, await this.transport.md5(this.file.slice(a, b)))}
    return this.md5s.get(n)!;
  }

  private async sendPart(n: number, signal: AbortSignal) {
    const [a, b] = partRange(this.session!, n);
    let attempt = 0;
    for (;;) {
      const digest = await this.digest(n);
      const cached = this.signed.get(n);
      if (!cached || cached.expiresAt <= this.options.now()) {
        // Sign this part plus a few queued ones in one request.
        const batch = [n, ...this.queue.slice(0, this.options.signBatch - 1)];
        for (const p of batch) await this.digest(p);
        await this.sign(batch, this.md5s);
      }
      this.inflight.set(n, 0);
      let result: PutResult;
      try {
        result = await this.transport.put(this.signed.get(n)!.url, this.file.slice(a, b), digest,
          loaded => {this.inflight.set(n, loaded); this.emit({})}, signal);
      } catch (e) {
        this.inflight.delete(n);
        if (signal.aborted) throw e;
        result = {status: 0};
      }
      this.inflight.delete(n);
      if (result.status >= 200 && result.status < 300) {
        this.done.add(n); this.signed.delete(n); this.emit({});
        // Optional registration; if it is lost the server re-reads storage anyway.
        this.transport.json(`/api/uploads/${this.session!.upload_id}/parts/${n}/done`, {method: 'POST'}).catch(() => undefined);
        return;
      }
      if (result.status === 403) {        // expired/invalid signature -> re-sign, bounded
        this.signed.delete(n);
        if (this.state.resigned + 1 > this.session!.part_count * 3) throw new UploadError('signature_rejected', '存储拒绝了上传签名，请稍后重试。');
        this.emit({resigned: this.state.resigned + 1});
        continue;
      }
      if (result.status === 400 && /BadDigest|InvalidDigest/.test(result.body || '')) {
        this.md5s.delete(n); this.signed.delete(n);  // the bytes read differed from the hash: re-read
      }
      if (++attempt > this.options.maxRetries) throw new UploadError('part_failed', `分片 ${n} 多次上传失败，请检查网络后继续。`);
      this.emit({retries: this.state.retries + 1});
      await this.transport.sleep(Math.min(30000, 500 * 2 ** attempt) * (0.5 + Math.random() / 2), signal);
    }
  }
}

const KEY = 'evireel:uploads';
function remember(s: UploadSession) {
  try {
    const all = JSON.parse(localStorage.getItem(KEY) || '{}');
    all[s.upload_id] = {name: s.original_name, size: s.declared_size, at: Date.now()};
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch { /* optional */ }
}
function forget(id: string) {
  try {const all = JSON.parse(localStorage.getItem(KEY) || '{}'); delete all[id]; localStorage.setItem(KEY, JSON.stringify(all))} catch { /* optional */ }
}

// Browser transport: XHR for upload progress, a Worker for MD5 so hashing never blocks the UI.
let worker: Worker | null = null; let nextId = 0;
const waiting = new Map<number, {resolve: (v: string) => void; reject: (e: Error) => void}>();
function md5InWorker(blob: Blob): Promise<string> {
  if (typeof Worker === 'undefined') return blob.arrayBuffer().then(b => toBase64(md5(new Uint8Array(b))));
  if (!worker) {
    worker = new Worker(new URL('./md5.worker.ts', import.meta.url), {type: 'module'});
    worker.onmessage = (e: MessageEvent<{id: number; md5?: string; error?: string}>) => {
      const job = waiting.get(e.data.id); waiting.delete(e.data.id);
      if (e.data.md5) job?.resolve(e.data.md5); else job?.reject(new Error(e.data.error || 'md5 failed'));
    };
  }
  const id = nextId++;
  return new Promise((resolve, reject) => {waiting.set(id, {resolve, reject}); worker!.postMessage({id, blob})});
}

export function browserTransport(json: Transport['json']): Transport {
  return {
    json, md5: md5InWorker,
    sleep: (ms, signal) => new Promise(resolve => {const t = setTimeout(resolve, ms); signal.addEventListener('abort', () => {clearTimeout(t); resolve()}, {once: true})}),
    put: (url, body, digest, onProgress, signal) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const abort = () => xhr.abort();
      if (signal.aborted) {reject(new DOMException('aborted', 'AbortError')); return}
      signal.addEventListener('abort', abort, {once: true});
      xhr.open('PUT', url);
      xhr.setRequestHeader('Content-MD5', digest);
      xhr.upload.onprogress = e => onProgress(e.loaded);
      xhr.onloadend = () => signal.removeEventListener('abort', abort);
      xhr.onload = () => resolve({status: xhr.status, body: xhr.responseText});
      xhr.onerror = () => resolve({status: 0});
      xhr.onabort = () => reject(new DOMException('aborted', 'AbortError'));
      xhr.send(body);
    }),
  };
}
