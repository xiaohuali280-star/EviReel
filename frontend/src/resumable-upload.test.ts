// Client scheduling logic against an in-memory fake server/storage (TEST DOUBLE).
// Proves retry, re-sign, pause/resume and "send only missing parts" decisions; it does
// not prove S3/MinIO behaviour - see tests/test_e1_* and e2e/upload-resume.spec.ts.
import {describe, expect, it} from 'vitest';
import {md5, toBase64} from './md5';
import {fingerprint, ResumableUpload, type Transport, UploadError, type UploadSession, type UploadState} from './resumable-upload';

const MiB = 1 << 20;
function makeFile(size: number, seed = 7) {
  const bytes = new Uint8Array(size);
  let x = seed;
  for (let i = 0; i < size; i++) {x = (x * 1103515245 + 12345) & 0x7fffffff; bytes[i] = x & 255}
  return new File([bytes], 'lecture.mp4', {type: 'video/mp4'});
}

class FakeServer implements Transport {
  stored = new Map<number, string>();  // part -> md5 actually stored
  puts: number[] = [];
  signCalls = 0; doneCalls = 0; completeCalls = 0;
  failPut = new Map<number, number>();      // part -> remaining network failures
  expireNext = new Map<number, number>();   // part -> remaining 403 responses
  corruptOnce = new Set<number>();          // part -> next PUT stored with a wrong digest
  dropDone = false;
  deleteFails = 0;                          // remaining DELETE network failures
  completeBeforeDelete = false;             // the session completes before the DELETE arrives
  deleteCalls = 0;
  clock = 0;
  session!: UploadSession;
  constructor(public partSize: number, public expiresIn = 900) {}
  md5 = async (blob: Blob) => toBase64(md5(new Uint8Array(await blob.arrayBuffer())));
  sleep = async () => {this.clock += 1};
  view(): UploadSession {
    const uploaded = [...this.stored.keys()].sort((a, b) => a - b);
    return {...this.session, uploaded_parts: uploaded,
      uploaded_part_md5: Object.fromEntries(uploaded.map(n => [String(n), this.stored.get(n)!])),
      missing_parts: Array.from({length: this.session.part_count}, (_, i) => i + 1).filter(n => !this.stored.has(n))};
  }
  async json<T>(url: string, init: {method?: string; body?: any} = {}): Promise<T> {
    if (url === '/api/uploads' && init.method === 'POST') {
      const size = init.body.size;
      this.session = {upload_id: 'up_' + '1'.repeat(32), video_id: 'v_' + '2'.repeat(32), state: 'uploading',
        original_name: init.body.filename, declared_size: size, part_size: this.partSize,
        part_count: Math.ceil(size / this.partSize), client_fingerprint: init.body.fingerprint,
        uploaded_parts: [], mismatched_parts: [], uploaded_bytes: 0, missing_parts: [], expires_at: '', error: null, result: null, job_id: null};
      return this.view() as T;
    }
    if (url.endsWith('/parts/sign')) {
      this.signCalls++;
      return {expires_in: this.expiresIn, parts: init.body.parts.map((p: any) => ({part_number: p.part_number, url: `put://${p.part_number}?md5=${p.md5_b64}&t=${this.clock}`}))} as T;
    }
    if (url.includes('/parts/') && url.endsWith('/done')) {
      this.doneCalls++;
      if (this.dropDone) throw new Error('registration lost');
      return this.view() as T;
    }
    if (url.endsWith('/complete')) {
      this.completeCalls++;
      if (this.view().missing_parts.length) throw new UploadError('parts_missing', 'missing');
      this.session = {...this.session, state: 'completed', result: {video_id: this.session.video_id}};
      return this.view() as T;
    }
    if (init.method === 'DELETE') {
      this.deleteCalls++;
      if (this.deleteFails > 0) {this.deleteFails--; throw new TypeError('Failed to fetch')}
      if (this.completeBeforeDelete) {
        this.session = {...this.session, state: 'completed', result: {video_id: this.session.video_id}};
        throw new UploadError('upload_not_cancellable', 'Upload is already completed');
      }
      this.session = {...this.session, state: 'aborted'}; return this.view() as T;
    }
    return this.view() as T;
  }
  async put(url: string, body: Blob, digest: string, onProgress: (n: number) => void, signal: AbortSignal) {
    const part = Number(url.slice(6).split('?')[0]);
    this.puts.push(part);
    if (signal.aborted) throw new DOMException('aborted', 'AbortError');
    if ((this.expireNext.get(part) || 0) > 0) {this.expireNext.set(part, this.expireNext.get(part)! - 1); return {status: 403, body: 'Request has expired'}}
    if ((this.failPut.get(part) || 0) > 0) {this.failPut.set(part, this.failPut.get(part)! - 1); return {status: 0}}
    const actual = await this.md5(body);
    if (this.corruptOnce.delete(part) || actual !== digest) return {status: 400, body: '<Code>BadDigest</Code>'};
    onProgress(body.size);
    this.stored.set(part, actual);
    return {status: 200};
  }
}

const run = (file: File, server: FakeServer, states: UploadState[] = []) =>
  new ResumableUpload(file, server, s => states.push(s), {concurrency: 3, maxRetries: 4, signBatch: 4, pollMs: 1, now: () => server.clock});

describe('resumable upload client', () => {
  it('uploads every part once and completes', async () => {
    const file = makeFile(5 * MiB + 123), server = new FakeServer(MiB);
    const states: UploadState[] = [];
    const result = await run(file, server, states).start();
    expect(result.state).toBe('completed');
    expect([...server.puts].sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(server.completeCalls).toBe(1);
    expect(states.at(-1)?.phase).toBe('completed');
    expect(states.at(-1)?.sentBytes).toBe(file.size);
  });

  it('resumes with the same file and sends only missing parts', async () => {
    const file = makeFile(6 * MiB), server = new FakeServer(MiB);
    const first = run(file, server);
    server.failPut.set(4, 99); server.failPut.set(5, 99); server.failPut.set(6, 99);
    await expect(first.start()).rejects.toThrow();
    const stored = [...server.stored.keys()].sort();
    expect(stored).toEqual([1, 2, 3]);
    server.failPut.clear(); server.puts = [];
    const reloaded = run(makeFile(6 * MiB), server);           // new File object after a browser refresh
    const result = await reloaded.start(server.view());
    expect(result.state).toBe('completed');
    expect([...server.puts].sort()).toEqual([4, 5, 6]);
  });

  it('refuses to resume with a different file', async () => {
    const server = new FakeServer(MiB);
    await run(makeFile(3 * MiB), server).start().catch(() => undefined);
    const other = run(makeFile(3 * MiB, 99), server);
    await expect(other.start({...server.view(), state: 'uploading'})).rejects.toMatchObject({code: 'file_mismatch'});
  });

  it('refuses to resume when the file changed outside the fingerprint samples, sending nothing', async () => {
    const size = 20 * MiB, server = new FakeServer(4 * MiB);
    const original = makeFile(size);
    server.failPut.set(4, 99); server.failPut.set(5, 99);
    await run(original, server).start().catch(() => undefined);
    expect([...server.stored.keys()].sort()).toEqual([1, 2, 3]);
    // Same size, same sampled ranges (head, tail, 25/50/75 %), different bytes at 2 MiB and 12.5 MiB.
    const bytes = new Uint8Array(await original.arrayBuffer());
    bytes[2 * MiB] ^= 0xff; bytes[12.5 * MiB] ^= 0xff;
    const edited = new File([bytes], 'lecture.mp4', {type: 'video/mp4'});
    expect(await fingerprint(edited)).toBe(await fingerprint(original));
    server.failPut.clear(); server.puts = []; const signs = server.signCalls;
    await expect(run(edited, server).start(server.view())).rejects.toMatchObject({code: 'file_mismatch'});
    expect(server.puts).toEqual([]);
    expect(server.signCalls).toBe(signs);
    expect(server.completeCalls).toBe(0);
    // The untouched original still resumes and sends only the missing parts.
    expect((await run(makeFile(size), server).start(server.view())).state).toBe('completed');
    expect([...server.puts].sort()).toEqual([4, 5]);
  });

  it('a cancel the server did not confirm is reported, not shown as cancelled, and can be retried', async () => {
    const server = new FakeServer(MiB), states: UploadState[] = [];
    const upload = run(makeFile(4 * MiB), server, states);
    server.failPut.set(3, 99);
    await upload.start().catch(() => undefined);
    server.deleteFails = 1;
    expect(await upload.cancel()).toBe('unconfirmed');
    expect(states.at(-1)?.phase).toBe('cancel_unconfirmed');
    expect(states.some(s => s.phase === 'cancelled')).toBe(false);
    expect(server.session.state).toBe('uploading');
    expect(await upload.cancel()).toBe('cancelled');
    expect(states.at(-1)?.phase).toBe('cancelled');
    expect(server.session.state).toBe('aborted');
    expect(server.deleteCalls).toBe(2);
  });

  it('a cancel that loses the race to complete follows the real server stage', async () => {
    const server = new FakeServer(MiB), states: UploadState[] = [];
    const upload = run(makeFile(3 * MiB), server, states);
    server.failPut.set(2, 99);
    await upload.start().catch(() => undefined);
    server.completeBeforeDelete = true;
    expect(await upload.cancel()).toBe('continuing');
    const result = await upload.wait();
    expect(result.state).toBe('completed');
    expect(states.at(-1)?.phase).toBe('completed');
    expect(states.some(s => s.phase === 'cancelled')).toBe(false);
  });

  it('re-signs expired URLs and retries transient failures', async () => {
    const server = new FakeServer(MiB);
    server.expireNext.set(2, 1); server.failPut.set(3, 2);
    const states: UploadState[] = [];
    const result = await run(makeFile(4 * MiB), server, states).start();
    expect(result.state).toBe('completed');
    expect(states.at(-1)?.resigned).toBe(1);
    expect(states.at(-1)?.retries).toBe(2);
    expect(server.signCalls).toBeGreaterThanOrEqual(2);
  });

  it('re-reads a part after BadDigest', async () => {
    const server = new FakeServer(MiB);
    server.corruptOnce.add(2);
    expect((await run(makeFile(3 * MiB), server).start()).state).toBe('completed');
    expect(server.puts.filter(p => p === 2).length).toBe(2);
  });

  it('completes even when every progress registration call is lost', async () => {
    const server = new FakeServer(MiB);
    server.dropDone = true;
    expect((await run(makeFile(3 * MiB), server).start()).state).toBe('completed');
    expect(server.doneCalls).toBe(3);
  });

  it('pauses without failing and resumes the remaining parts', async () => {
    const server = new FakeServer(MiB);
    const states: UploadState[] = [];
    const upload = run(makeFile(8 * MiB), server, states);
    const original = server.put.bind(server);
    let paused = false;
    server.put = async (...args) => {
      const result = await original(...args);
      if (!paused && server.stored.size >= 2) {paused = true; upload.pause()}
      return result;
    };
    await upload.start();
    expect(states.some(s => s.phase === 'paused')).toBe(true);
    expect(server.stored.size).toBeLessThan(8);
    const before = new Set(server.stored.keys());
    server.puts = [];
    expect((await upload.resume()).state).toBe('completed');
    expect(server.puts.some(p => before.has(p))).toBe(false);
  });

  it('fingerprint depends on content, not on the File object', async () => {
    expect(await fingerprint(makeFile(3 * MiB))).toBe(await fingerprint(makeFile(3 * MiB)));
    expect(await fingerprint(makeFile(3 * MiB))).not.toBe(await fingerprint(makeFile(3 * MiB, 8)));
  });
});
