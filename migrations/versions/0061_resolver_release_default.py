"""Default new contests to resolver without changing saved release policies."""
from alembic import op
import sqlalchemy as sa

revision = "0061_resolver_release_default"
down_revision = "0060_hide_prestart_counts"
branch_labels = depends_on = None


def upgrade():
    # SQLite uses ORM insertion defaults. Rebuilding its referenced contests
    # table only to change a default could affect existing contest records.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "scoreboard_release_mode",
                        existing_type=sa.String(32), server_default="resolver")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "scoreboard_release_mode",
                        existing_type=sa.String(32), server_default="manual")
