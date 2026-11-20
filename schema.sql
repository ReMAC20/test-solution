CREATE TABLE IF NOT EXISTS crm_sync_runs (
    seq BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL UNIQUE,
    is_full BOOLEAN NOT NULL,
    status TEXT NOT NULL DEFAULT 'loading'
        CHECK (status IN ('loading', 'ready', 'completed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    customers_count BIGINT NOT NULL DEFAULT 0,
    leads_count BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crm_sync_customers (
    run_id UUID NOT NULL REFERENCES crm_sync_runs(run_id) ON DELETE CASCADE,
    crm_id BIGINT NOT NULL,
    login TEXT NOT NULL,
    payload JSONB NOT NULL,
    contacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    companies JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, crm_id)
);

CREATE TABLE IF NOT EXISTS crm_sync_leads (
    run_id UUID NOT NULL REFERENCES crm_sync_runs(run_id) ON DELETE CASCADE,
    crm_id BIGINT NOT NULL,
    login TEXT,
    payload JSONB NOT NULL,
    contacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, crm_id)
);

CREATE INDEX IF NOT EXISTS ix_crm_sync_customers_login
    ON crm_sync_customers (run_id, login);
CREATE INDEX IF NOT EXISTS ix_crm_sync_leads_login
    ON crm_sync_leads (run_id, login);

CREATE INDEX IF NOT EXISTS ix_abstract_customers_gin
    ON abstract_customer USING GIN (amo_customers jsonb_path_ops);
CREATE INDEX IF NOT EXISTS ix_abstract_leads_gin
    ON abstract_customer USING GIN (amo_leads jsonb_path_ops);
