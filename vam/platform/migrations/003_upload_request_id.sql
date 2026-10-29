-- Idempotent session creation. The client sends a random request id with POST /api/uploads;
-- replaying it returns the same session, so a client whose create response was lost (or who
-- cancelled while it was in flight) can find and cancel what was created instead of guessing.
ALTER TABLE upload_sessions ADD COLUMN client_request_id text
  CHECK (client_request_id IS NULL OR client_request_id ~ '^[A-Za-z0-9-]{8,64}$');
CREATE UNIQUE INDEX upload_sessions_request ON upload_sessions(workspace_id, client_request_id)
  WHERE client_request_id IS NOT NULL;
