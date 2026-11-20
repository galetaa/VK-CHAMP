from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_name("0001_schema.sql").read_text(encoding="utf-8")
    sql = sql.replace("\nBEGIN;\n", "\n", 1).removesuffix("COMMIT;\n")
    op.get_bind().connection.driver_connection.execute(sql)
    op.execute(
        """
        CREATE TABLE vk_demo.worker_heartbeat (
            worker_name text PRIMARY KEY,
            seen_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE vk_demo.rate_limits (
            scope text PRIMARY KEY,
            tokens double precision NOT NULL,
            sampled_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE vk_demo.simulation_locks (
            sandbox_id uuid PRIMARY KEY REFERENCES vk_demo.sandboxes ON DELETE CASCADE,
            execution_id uuid NOT NULL,
            expires_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'casechamp_app') THEN
                GRANT USAGE ON SCHEMA vk_demo TO casechamp_app;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA vk_demo TO casechamp_app;
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vk_demo TO casechamp_app;
                GRANT SELECT ON TABLE public.alembic_version TO casechamp_app;
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'casechamp_maintenance') THEN
                GRANT USAGE ON SCHEMA vk_demo TO casechamp_maintenance;
                GRANT SELECT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vk_demo TO casechamp_maintenance;
            END IF;
        END
        $$;
        """
    )


def downgrade():
    op.execute("DROP SCHEMA vk_demo CASCADE")
