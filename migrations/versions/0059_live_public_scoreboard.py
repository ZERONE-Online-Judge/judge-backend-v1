"""Separate anonymous live scoreboard access from archive visibility."""
from alembic import op
import sqlalchemy as sa

revision = "0059_live_public_scoreboard"
down_revision = "0058_hidden_progress_default"
branch_labels = depends_on = None


def upgrade():
    if "scoreboard_public_during_contest" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("contests")
    }:
        op.add_column("contests", sa.Column(
            "scoreboard_public_during_contest", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))


def downgrade():
    op.drop_column("contests", "scoreboard_public_during_contest")
