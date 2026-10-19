"""Durable video-bound conversations; terminal writes and idempotency are atomic."""
from __future__ import annotations

import json
import re
import time
import uuid

from vam.video_registry import DomainError


class ConversationStore:
    def __init__(self, registry):
        self.registry, self.db = registry, registry.db

    def create(self, video_id):
        row = self.registry.get(video_id)
        if row['trashed_at'] is not None:
            raise DomainError(409, 'video_trashed', 'Restore the video before creating a conversation')
        if row['status'] in {'deleting', 'delete_failed'}:
            raise DomainError(409, 'video_deleting', 'Video is being deleted')
        cid, now = 'c_' + uuid.uuid4().hex, time.time()
        with self.db.connect(write=True) as conn:
            conn.execute('INSERT INTO conversations(conversation_id,video_id,created_at,updated_at) VALUES(?,?,?,?)',
                         (cid, video_id, now, now))
        return self.get(cid)

    def get(self, cid, video_id=None):
        if not re.fullmatch(r'c_[0-9a-f]{32}', str(cid)):
            raise DomainError(404, 'conversation_not_found', 'Conversation not found')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM conversations WHERE conversation_id=?', (cid,)).fetchone()
        if row is None:
            raise DomainError(404, 'conversation_not_found', 'Conversation not found')
        if video_id is not None and row['video_id'] != video_id:
            raise DomainError(409, 'conversation_video_mismatch', 'Conversation belongs to a different video')
        return dict(row)

    def list(self, video_id, offset=0, limit=50):
        self.registry.get(video_id)
        with self.db.connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM conversations WHERE video_id=?', (video_id,)).fetchone()[0]
            rows = conn.execute("SELECT c.*,COALESCE((SELECT substr(content,1,80) FROM messages m WHERE m.conversation_id=c.conversation_id AND m.role='user' ORDER BY message_id LIMIT 1),'新对话') AS title FROM conversations c WHERE video_id=? ORDER BY updated_at DESC,conversation_id LIMIT ? OFFSET ?",
                                (video_id, limit, offset)).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total, 'offset': offset, 'limit': limit}

    def request(self, cid, rid):
        self.get(cid)
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM conversation_requests WHERE conversation_id=? AND request_id=?', (cid, rid)).fetchone()
        if row is None:
            raise DomainError(404, 'request_not_found', 'Request not found')
        result = dict(row)
        for field in ('result', 'resolution'):
            result[field] = json.loads(result[field]) if result[field] else None
        result.pop('payload', None)
        return result

    def accept(self, cid, rid, video_id, payload, original, capacity):
        self.get(cid, video_id)
        if not re.fullmatch(r'r_[0-9a-f]{32}', str(rid)):
            raise DomainError(422, 'invalid_request_id', 'Invalid request_id')
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        now = time.time()
        with self.db.connect(write=True) as conn:
            previous = conn.execute('SELECT payload FROM conversation_requests WHERE conversation_id=? AND request_id=?', (cid, rid)).fetchone()
            if previous:
                if previous['payload'] != canonical:
                    raise DomainError(409, 'request_conflict', 'request_id already has different input')
                return False
            if conn.execute("SELECT 1 FROM conversation_requests WHERE conversation_id=? AND status IN ('accepted','running')", (cid,)).fetchone():
                raise DomainError(409, 'conversation_busy', 'Wait for the current conversation request')
            if conn.execute("SELECT COUNT(*) FROM conversation_requests WHERE status IN ('accepted','running')").fetchone()[0] >= capacity:
                raise DomainError(429, 'conversation_capacity', 'Question capacity reached; retry later')
            conn.execute("INSERT INTO conversation_requests(conversation_id,request_id,payload,original_text,status,created_at,updated_at) VALUES(?,?,?,?,'accepted',?,?)",
                         (cid, rid, canonical, original, now, now))
            conn.execute("INSERT INTO messages(conversation_id,request_id,role,content,created_at) VALUES(?,?,'user',?,?)", (cid, rid, original, now))
            conn.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=?', (now, cid))
        return True

    def resolved(self, cid, rid, resolution):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE conversation_requests SET status='running',rewritten_question=?,resolution=?,updated_at=? WHERE conversation_id=? AND request_id=? AND status IN ('accepted','running')",
                         (resolution['rewritten_question'], json.dumps(resolution, ensure_ascii=False), time.time(), cid, rid))

    def finish(self, cid, rid, event):
        now = time.time()
        with self.db.connect(write=True) as conn:
            row = conn.execute('SELECT status FROM conversation_requests WHERE conversation_id=? AND request_id=?', (cid, rid)).fetchone()
            if row is None or row['status'] not in {'accepted', 'running'}:
                return False
            conn.execute("UPDATE conversation_requests SET status='completed',result=?,updated_at=? WHERE conversation_id=? AND request_id=?",
                         (json.dumps(event, ensure_ascii=False, allow_nan=False), now, cid, rid))
            conn.execute("INSERT INTO messages(conversation_id,request_id,role,content,created_at) VALUES(?,?,'assistant',?,?)",
                         (cid, rid, str(event.get('text') or ''), now))
            conn.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=?', (now, cid))
        return True

    def fail(self, cid, rid, code, status='failed'):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE conversation_requests SET status=?,error_code=?,updated_at=? WHERE conversation_id=? AND request_id=? AND status IN ('accepted','running')",
                         (status, code, time.time(), cid, rid))

    def recover(self):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE conversation_requests SET status='interrupted',error_code='server_restart',updated_at=? WHERE status IN ('accepted','running')", (time.time(),))

    def delete(self, cid):
        self.get(cid)
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM conversation_requests WHERE conversation_id=? AND status IN ('accepted','running')", (cid,)).fetchone():
                raise DomainError(409, 'conversation_busy', 'Cannot delete a running conversation')
            conn.execute('DELETE FROM conversations WHERE conversation_id=?', (cid,))

    def turns(self, cid, *, limit, before=None, after=0, ascending=False):
        order = 'ASC' if ascending else 'DESC'
        with self.db.connect() as conn:
            rows = conn.execute(f"""SELECT u.message_id AS user_message_id,a.message_id,u.content AS question,
                a.content AS answer,r.result FROM messages a
                JOIN messages u ON u.conversation_id=a.conversation_id AND u.request_id=a.request_id AND u.role='user'
                JOIN conversation_requests r ON r.conversation_id=a.conversation_id AND r.request_id=a.request_id
                WHERE a.conversation_id=? AND a.role='assistant' AND r.status='completed'
                AND a.message_id>? AND (? IS NULL OR a.message_id<?)
                ORDER BY a.message_id {order} LIMIT ?""", (cid, after, before, before, limit)).fetchall()
        turns = [dict(row) | {'result': json.loads(row['result'])} for row in rows]
        return turns if ascending else list(reversed(turns))

    def save_summary(self, cid, through, text=None):
        with self.db.connect(write=True) as conn:
            if text is None:
                conn.execute("UPDATE conversations SET summary_attempt_through=?,summary_status='failed' WHERE conversation_id=?", (through, cid))
            else:
                conn.execute("UPDATE conversations SET summary=?,summary_through=?,summary_attempt_through=?,summary_status='ready' WHERE conversation_id=?",
                             (text, through, through, cid))

    def mark_snapshot(self, event, video_id):
        """Annotate a copy; a historical ID never resolves to a replacement record."""
        if event is None:
            return None
        copied = json.loads(json.dumps(event))
        row = self.registry.get(video_id)
        state = self.registry.transcript_state(row)
        for item in copied.get('result', {}).get('evidence', []):
            current = state.get('search_version') if item.get('source') == 'transcript' else row['active_generation']
            item['historical_snapshot'] = True
            item['stale_evidence'] = item.get('index_version') != current
        return copied

    def messages(self, cid, after=0, limit=50):
        conversation = self.get(cid)
        with self.db.connect() as conn:
            rows = conn.execute('SELECT m.*,r.rewritten_question,r.resolution,r.status,r.error_code,r.result FROM messages m JOIN conversation_requests r USING(conversation_id,request_id) WHERE m.conversation_id=? AND message_id>? ORDER BY message_id LIMIT ?', (cid, after, limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item['resolution'] = json.loads(item['resolution']) if item['resolution'] else None
            item['result'] = self.mark_snapshot(json.loads(item['result']), conversation['video_id']) if item['role'] == 'assistant' and item['result'] else None
            items.append(item)
        return {'items': items, 'next_after': items[-1]['message_id'] if len(rows) > limit else None}
