"""Persist stable, unique short links for contests."""

from alembic import op
import sqlalchemy as sa

revision = "0056_contest_share_links"
down_revision = "0055_board_writing_policy"
branch_labels = depends_on = None


def upgrade():
    if "contest_share_links" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "contest_share_links",
            sa.Column("code", sa.String(11), primary_key=True),
            sa.Column(
                "contest_id",
                sa.String(36),
                sa.ForeignKey("contests.contest_id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
        )


def downgrade():
    op.drop_table("contest_share_links")
