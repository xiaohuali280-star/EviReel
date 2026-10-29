-- E2: sequence of the pipeline job whose results are currently published for this video.
-- Worker publications are conditional on it (an older job can never overwrite a newer one).
ALTER TABLE videos ADD COLUMN published_seq INTEGER NOT NULL DEFAULT 0;
