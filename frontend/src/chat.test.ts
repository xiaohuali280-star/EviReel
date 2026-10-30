// ChatChannel send/reconcile timing against a fake WebSocket and a mocked status API (TEST DOUBLE).
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';

const status = vi.fn();
vi.mock('./api', () => {
  class ApiError extends Error {constructor(public code: string, message: string, public status: number) {super(message)}}
  return {ApiError, api: (...args: unknown[]) => status(...args)};
});

class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket;
  readyState = 1;
  sent: string[] = [];
  onopen?: () => void; onmessage?: (e: {data: string}) => void; onclose?: () => void; onerror?: () => void;
  constructor() {FakeSocket.last = this; setTimeout(() => this.onopen?.(), 0)}
  send(data: string) {this.sent.push(data)}
  close() {this.readyState = 3}
}

const payload = {type: 'text', video_id: 'v_' + '1'.repeat(32), conversation_id: 'c1',
                 request_id: 'r_' + '2'.repeat(32), text: 'hi'} as never;

describe('ChatChannel', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('location', {protocol: 'http:', host: '127.0.0.1'});
    vi.stubGlobal('localStorage', {getItem: () => null, setItem: () => {}, removeItem: () => {}});
    status.mockReset();
  });
  afterEach(() => {vi.useRealTimers(); vi.unstubAllGlobals()});

  it('does not race its own frame: no status check (and no resend) right after sending', async () => {
    const {ChatChannel, RECONCILE_MS} = await import('./chat');
    const {ApiError} = await import('./api');
    status.mockRejectedValue(new ApiError('not_found', 'missing', 404));   // server has not registered it yet
    const channel = new ChatChannel('v_' + '1'.repeat(32), 'c1', () => {}, () => {});
    await vi.advanceTimersByTimeAsync(1);
    channel.send(payload);
    await vi.advanceTimersByTimeAsync(RECONCILE_MS - 1);
    expect(status).not.toHaveBeenCalled();
    expect(FakeSocket.last.sent).toHaveLength(1);
    channel.close();
  });

  it('still resends a request the server confirms missing, with the same request id', async () => {
    const {ChatChannel, RECONCILE_MS} = await import('./chat');
    const {ApiError} = await import('./api');
    status.mockRejectedValueOnce(new ApiError('not_found', 'missing', 404)).mockResolvedValue({status: 'running', result: null});
    const channel = new ChatChannel('v_' + '1'.repeat(32), 'c1', () => {}, () => {});
    await vi.advanceTimersByTimeAsync(1);
    channel.send(payload);
    await vi.advanceTimersByTimeAsync(RECONCILE_MS + 1);
    expect(status).toHaveBeenCalledTimes(1);
    const frames = FakeSocket.last.sent.map(s => JSON.parse(s).request_id);
    expect(frames).toEqual([payload['request_id' as never], payload['request_id' as never]]);
    channel.close();
  });
});
