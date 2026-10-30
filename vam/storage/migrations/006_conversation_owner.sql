-- E3: question-answering requests are owned by the API instance executing them. The owner renews
-- lease_until while it runs; recovery interrupts only requests whose owner stopped renewing, so one
-- instance starting up never interrupts the running questions of another.
ALTER TABLE conversation_requests ADD COLUMN owner TEXT;
ALTER TABLE conversation_requests ADD COLUMN lease_until REAL;
CREATE INDEX idx_conversation_requests_active_lease ON conversation_requests(status, lease_until)
  WHERE status IN ('accepted','running');
