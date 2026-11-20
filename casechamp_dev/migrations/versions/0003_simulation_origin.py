from alembic import op

revision = "0003_simulation_origin"
down_revision = "0002_fixture"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE vk_demo.scenario_revisions ADD COLUMN is_simulation boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE vk_demo.simulation_locks ADD COLUMN recovery jsonb NOT NULL DEFAULT '{}'::jsonb")


def downgrade():
    op.execute("ALTER TABLE vk_demo.simulation_locks DROP COLUMN recovery")
    op.execute("ALTER TABLE vk_demo.scenario_revisions DROP COLUMN is_simulation")
