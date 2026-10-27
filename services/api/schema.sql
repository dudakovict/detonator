-- One row per unique file, keyed by its SHA-256. Applied at API startup.
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
