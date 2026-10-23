"""Add question privacy and comment-writing policies with compatible defaults."""

from alembic import op
import sqlalchemy as sa

revision = "0055_board_writing_policy"
down_revision = "0054_account_passkeys"
branch_labels = depends_on = None

FIELDS = (
    ("contests", "board_question_visibility_policy", "choice"),
    ("contests", "board_question_default_visibility", "public"),
    ("contests", "board_comment_scope_policy", "choice"),
    ("contests", "board_comment_default_scope", "all"),
    ("contest_questions", "comment_scope", "all"),
)


def upgrade():
    for table, field, default in FIELDS:
        if field not in {
            column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)
        }:
            op.add_column(
                table,
                sa.Column(field, sa.String(16), nullable=False, server_default=default),
            )


def downgrade():
    for table, field, _ in reversed(FIELDS):
        op.drop_column(table, field)
