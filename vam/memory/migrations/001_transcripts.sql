CREATE TABLE IF NOT EXISTS transcript_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO transcript_schema VALUES(1,1);
CREATE TABLE IF NOT EXISTS transcript_versions (
 version TEXT PRIMARY KEY, video_id TEXT NOT NULL, transcription_key TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, asr_fingerprint TEXT NOT NULL, chunk_config TEXT NOT NULL,
 embedding_fingerprint TEXT NOT NULL, dim INTEGER, language TEXT,
 status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcription_key ON transcript_versions(video_id,transcription_key);
CREATE TABLE IF NOT EXISTS transcript_segments (
 version TEXT NOT NULL REFERENCES transcript_versions(version) ON DELETE CASCADE,
 segment_id TEXT NOT NULL, start_t REAL NOT NULL, end_t REAL NOT NULL,
 text TEXT NOT NULL, language TEXT, quality TEXT NOT NULL, raw_text TEXT NOT NULL,
 raw_start_t REAL, raw_end_t REAL, time_clipped INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(version,segment_id)
);
CREATE TABLE IF NOT EXISTS transcript_chunks (
 version TEXT NOT NULL REFERENCES transcript_versions(version) ON DELETE CASCADE,
 doc_id TEXT NOT NULL, start_t REAL NOT NULL, end_t REAL NOT NULL,
 text TEXT NOT NULL, segment_ids TEXT NOT NULL, embedding BLOB,
 PRIMARY KEY(version,doc_id)
);
CREATE INDEX IF NOT EXISTS idx_transcript_time ON transcript_chunks(version,start_t,end_t);
CREATE TABLE IF NOT EXISTS transcript_heads (
 video_id TEXT PRIMARY KEY,
 text_version TEXT REFERENCES transcript_versions(version),
 search_version TEXT REFERENCES transcript_versions(version)
);
