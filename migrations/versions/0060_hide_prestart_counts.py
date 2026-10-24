"""Allow hiding participation totals before a contest starts."""
from alembic import op
import sqlalchemy as sa

revision = "0060_hide_prestart_counts"
down_revision = "0059_live_public_scoreboard"
branch_labels = depends_on = None


def upgrade():
    if "hide_participation_counts_before_start" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("contests")
    }:
        op.add_column("contests", sa.Column(
            "hide_participation_counts_before_start", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))


def downgrade():
    op.drop_column("contests", "hide_participation_counts_before_start")
