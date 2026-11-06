-- Existing databases: run with psql, outside a transaction.
-- Fresh databases: db.create_all() creates the same indexes from models.py.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_abstract_customer_amo_customers_gin
    ON abstract_customer USING gin (amo_customers jsonb_path_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_abstract_customer_amo_leads_gin
    ON abstract_customer USING gin (amo_leads jsonb_path_ops);
