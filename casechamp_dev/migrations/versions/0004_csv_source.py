from alembic import op

revision = "0004_csv_source"
down_revision = "0003_simulation_origin"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE vk_demo.catalog_imports ADD COLUMN source_content bytea
        CHECK (octet_length(source_content) <= 2097152)
    """)


def downgrade():
    op.execute("ALTER TABLE vk_demo.catalog_imports DROP COLUMN source_content")
