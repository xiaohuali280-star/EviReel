CREATE TABLE conversations (
 conversation_id TEXT PRIMARY KEY,
 video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 summary TEXT NOT NULL DEFAULT '', summary_through INTEGER NOT NULL DEFAULT 0,
 summary_attempt_through INTEGER NOT NULL DEFAULT 0, summary_status TEXT NOT NULL DEFAULT 'empty'
);
CREATE INDEX idx_conversations_video ON conversations(video_id,updated_at DESC);
CREATE TABLE conversation_requests (
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, payload TEXT NOT NULL, original_text TEXT NOT NULL,
 rewritten_question TEXT, resolution TEXT, status TEXT NOT NULL,
 result TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(conversation_id,request_id)
);
CREATE UNIQUE INDEX idx_conversation_active ON conversation_requests(conversation_id)
 WHERE status IN ('accepted','running');
CREATE TABLE messages (
 message_id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
 content TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(conversation_id,request_id,role),
 FOREIGN KEY(conversation_id,request_id) REFERENCES conversation_requests(conversation_id,request_id) ON DELETE CASCADE
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id,message_id);
