import {api, ApiError} from './api';
import type {AgentEvent, ChatPayload} from './types';

export const matches = (e: AgentEvent, p: ChatPayload) => e.video_id === p.video_id && e.conversation_id === p.conversation_id && e.request_id === p.request_id;
export const pendingKey = (cid: string) => `evireel:pending:${cid}`;
export function remembered(cid: string): ChatPayload | null {
  try {
    const p = JSON.parse(localStorage.getItem(pendingKey(cid)) || 'null');
    return p && p.conversation_id === cid && /^r_[0-9a-f]{32}$/.test(p.request_id) && /^v_[0-9a-f]{32}$/.test(p.video_id) && typeof p.text === 'string' ? p : null;
  } catch {return null}
}
/** Reconnect reuses the exact request ID. HTTP reconciliation covers a final
 * event lost between socket disconnect and subscription replay. */
export class ChatChannel {
  socket: WebSocket | null = null;
  payload: ChatPayload | null = null;
  stopped = false;
  attempt = 0;
  reconnect: ReturnType<typeof setTimeout> | undefined;
  poll: ReturnType<typeof setTimeout> | undefined;
  abort = new AbortController();
  constructor(public video: string, public cid: string, public receive: (e: AgentEvent) => void, public connection: (s: string) => void) {this.open()}
  open() {
    if (this.stopped) return;
    this.connection('connecting');
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/agent`);
    this.socket = ws;
    ws.onopen = () => {
      if (this.stopped) {ws.close(); return}
      this.attempt = 0; this.connection('online');
      if (this.payload) ws.send(JSON.stringify(this.payload));
    };
    ws.onmessage = e => {
      let value: AgentEvent;
      try {value = JSON.parse(e.data)} catch {return}
      if (!value || typeof value !== 'object' || typeof value.type !== 'string') return;
      if (this.payload && matches(value, this.payload)) this.event(value);
    };
    ws.onclose = () => {
      if (this.stopped) return;
      this.connection('offline');
      this.reconnect = setTimeout(() => this.open(), Math.min(15000, 500 * 2 ** this.attempt++));
    };
    ws.onerror = () => ws.close();
  }
  send(payload: ChatPayload) {
    if (this.payload || payload.video_id !== this.video || payload.conversation_id !== this.cid) return false;
    this.payload = payload;
    try {localStorage.setItem(pendingKey(this.cid), JSON.stringify(payload))} catch { /* socket recovery still works without storage */ }
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(payload));
    this.reconcile();
    return true;
  }
  event(e: AgentEvent) {
    if (this.stopped || !this.payload || !matches(e, this.payload)) return;
    if (['final', 'error'].includes(e.type)) {
      try {localStorage.removeItem(pendingKey(this.cid))} catch { /* optional browser persistence */ }
      this.payload = null;
      clearTimeout(this.poll);
    }
    this.receive(e);
  }
  async reconcile() {
    const p = this.payload;
    if (!p || this.stopped) return;
    try {
      const row = await api<{status: string; result: AgentEvent | null; error_code?: string}>(`/api/conversations/${this.cid}/requests/${p.request_id}`, {signal: this.abort.signal});
      if (this.payload?.request_id !== p.request_id || this.stopped) return;
      if (row.status === 'completed' && row.result) this.event({...row.result, video_id: p.video_id, conversation_id: this.cid, request_id: p.request_id});
      else if (['failed', 'interrupted'].includes(row.status)) this.event({...p, type: 'error', code: row.error_code, status: row.status});
    } catch (error) {
      // An open socket may have lost the outbound frame during a network change.
      // Only a confirmed missing request is resent, always with its original ID.
      if (error instanceof ApiError && error.status === 404 && this.payload?.request_id === p.request_id && this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(p));
    }
    if (this.payload && !this.stopped) this.poll = setTimeout(() => this.reconcile(), 1500);
  }
  close() {this.stopped = true; clearTimeout(this.reconnect); clearTimeout(this.poll); this.abort.abort(); this.socket?.close()}
}
