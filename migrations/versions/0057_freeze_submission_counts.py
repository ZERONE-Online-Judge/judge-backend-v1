"""Separate frozen submission counts from result release policy."""
from alembic import op
import sqlalchemy as sa

revision = "0057_freeze_submission_counts"
down_revision = "0056_contest_share_links"
branch_labels = depends_on = None


def upgrade():
    if "freeze_submission_count_visible" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("contests")
    }:
        op.add_column("contests", sa.Column(
            "freeze_submission_count_visible", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))


def downgrade():
    op.drop_column("contests", "freeze_submission_count_visible")
