-- E3: published files of each publication (index generations, playback) stored as immutable objects;
-- an instance with a missing or new DATA_ROOT rebuilds its local copy from these rows (sha256-verified).
CREATE TABLE publication_objects (
 video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
 relative_path TEXT NOT NULL, object_key TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 published_seq INTEGER NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(video_id, relative_path)
);
