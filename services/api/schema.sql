-- One row per unique file, keyed by its SHA-256. Applied at API startup, so
-- every statement must be safe to run again.
CREATE TABLE IF NOT EXISTS samples (
    sha256       char(64)    PRIMARY KEY,
    filename     text        NOT NULL,
    size         bigint      NOT NULL,
    status       text        NOT NULL DEFAULT 'queued',  -- queued | running | done | error
    verdict      text,                                    -- clean | suspicious | malicious
    error        text,
    submitted_at timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE samples ADD COLUMN IF NOT EXISTS score integer;

-- Indicators of compromise found by the worker, for lookups across samples
-- ("which samples contacted this domain?").
CREATE TABLE IF NOT EXISTS iocs (
    sha256 char(64) NOT NULL REFERENCES samples ON DELETE CASCADE,
    type   text     NOT NULL,  -- domains | urls | ips | sha256
    value  text     NOT NULL,
    PRIMARY KEY (sha256, type, value)
);
CREATE INDEX IF NOT EXISTS iocs_value ON iocs (value);
