from alembic import op

revision = "0002_fixture"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE vk_demo.sandboxes ADD COLUMN fixture_id text NOT NULL DEFAULT 'TABLE_DEMO_V1'
        CHECK (fixture_id IN ('TABLE_DEMO_V1', 'EMPTY_V1'))
    """)


def downgrade():
    op.execute("ALTER TABLE vk_demo.sandboxes DROP COLUMN fixture_id")
