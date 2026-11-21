from alembic import op
from app.settings import ROOT

revision = "0001_v2"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA vk_demo_v2")
    op.execute("REVOKE ALL ON SCHEMA vk_demo_v2 FROM PUBLIC")
    op.execute("SET LOCAL search_path TO vk_demo_v2, pg_catalog")
    ddl = (ROOT / "packages/contracts_v2/database.sql").read_text(encoding="utf-8")
    statements = ddl.replace("\nBEGIN;\n", "\n", 1).strip().removesuffix("COMMIT;")
    op.get_bind().connection.driver_connection.execute(statements)
    op.execute("""
        CREATE TABLE worker_heartbeat (id boolean PRIMARY KEY CHECK(id), seen_at timestamptz NOT NULL);
        CREATE TABLE rate_windows (
            scope text NOT NULL, starts_at timestamptz NOT NULL,
            requests integer NOT NULL CHECK(requests>0), PRIMARY KEY(scope,starts_at)
        );
        CREATE TABLE event_delivery (
            sandbox_id uuid NOT NULL, generation integer NOT NULL, event_id uuid NOT NULL,
            delivered_at timestamptz NOT NULL, PRIMARY KEY(sandbox_id,generation,event_id),
            FOREIGN KEY(sandbox_id,generation,event_id) REFERENCES events(sandbox_id,generation,event_id)
        );
        CREATE INDEX order_buyer_lookup ON orders(sandbox_id,generation,buyer_id,id);
        CREATE INDEX quote_buyer_lookup ON quotes(sandbox_id,generation,buyer_id,id);
        CREATE INDEX receipt_expiry ON idempotency_receipts(expires_at);
        CREATE INDEX outbox_due ON outbox(next_attempt_at,id) WHERE delivered_at IS NULL;
        CREATE FUNCTION preserve_order_terms() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (to_jsonb(OLD) - ARRAY['status','revision','accepted_at','fulfilled_at','cancelled_at','cancel_reason'])
             IS DISTINCT FROM
             (to_jsonb(NEW) - ARRAY['status','revision','accepted_at','fulfilled_at','cancelled_at','cancel_reason']) THEN
             RAISE EXCEPTION 'immutable order terms' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER order_terms_immutable BEFORE UPDATE ON orders
            FOR EACH ROW EXECUTE FUNCTION preserve_order_terms();
        CREATE FUNCTION preserve_catalog_pack() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.units_per_pack IS DISTINCT FROM OLD.units_per_pack THEN
             RAISE EXCEPTION 'immutable pack units' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER catalog_pack_immutable BEFORE UPDATE ON catalog_skus
            FOR EACH ROW EXECUTE FUNCTION preserve_catalog_pack();
        CREATE FUNCTION preserve_offer_mode() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.mode IS DISTINCT FROM OLD.mode THEN
             RAISE EXCEPTION 'immutable offer mode' USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER offer_mode_immutable BEFORE UPDATE ON offers
            FOR EACH ROW EXECUTE FUNCTION preserve_offer_mode();
        """)
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='casechamp_app') THEN
            GRANT USAGE ON SCHEMA vk_demo_v2 TO casechamp_app;
            GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA vk_demo_v2 TO casechamp_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vk_demo_v2 TO casechamp_app;
            GRANT SELECT ON TABLE public.alembic_version_v2 TO casechamp_app;
            GRANT DELETE ON idempotency_receipts,rate_windows TO casechamp_app;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='casechamp_maintenance') THEN
            GRANT USAGE ON SCHEMA vk_demo_v2 TO casechamp_maintenance;
            GRANT SELECT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vk_demo_v2 TO casechamp_maintenance;
          END IF;
        END $$;
        """)


def downgrade():
    op.execute("DROP SCHEMA vk_demo_v2 CASCADE")
