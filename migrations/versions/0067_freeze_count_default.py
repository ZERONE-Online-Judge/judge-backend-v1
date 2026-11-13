"""Show frozen submission counts in new contests, preserving saved settings."""
from alembic import op
import sqlalchemy as sa

revision = "0067_freeze_count_default"
down_revision = "0066_ioi_subtasks"
branch_labels = depends_on = None


def upgrade():
    # Do not rebuild SQLite's referenced contests table for a default change.
    # ORM inserts use the new default; existing records remain untouched.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "freeze_submission_count_visible",
                        existing_type=sa.Boolean(), server_default=sa.true())


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "freeze_submission_count_visible",
                        existing_type=sa.Boolean(), server_default=sa.false())
