"""Persist full-suite operator verification evidence.

Revision ID: 0052_verification_executions
Revises: 0051_allowed_languages
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_verification_executions"
down_revision = "0051_allowed_languages"
branch_labels = None
depends_on = None


def upgrade():
    if "verification_executions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("verification_executions",
        sa.Column("submission_id", sa.String(36), primary_key=True),
        sa.Column("problem_id", sa.String(36), nullable=False),
        sa.Column("testcases", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("storage_keys", sa.JSON(), nullable=False),
        sa.Column("testcase_version", sa.Integer(), nullable=True),
        sa.Column("full_suite_complete", sa.Boolean(), nullable=False),
        sa.Column("node_name", sa.String(120), nullable=True),
        sa.Column("agent_version", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True))
    op.create_index("ix_verification_executions_problem_id", "verification_executions", ["problem_id"])


def downgrade():
    op.drop_table("verification_executions")
