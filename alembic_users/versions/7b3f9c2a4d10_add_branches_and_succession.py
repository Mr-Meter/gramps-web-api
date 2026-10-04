"""Add user branches and succession tables

Revision ID: 7b3f9c2a4d10
Revises: d4e9a1c7b3f2
Create Date: 2026-10-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

from gramps_webapi.auth.sql_guid import GUID

# revision identifiers, used by Alembic.
revision = "7b3f9c2a4d10"
down_revision = "d4e9a1c7b3f2"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    tables = Inspector.from_engine(conn).get_table_names()

    if "user_branches" not in tables:
        op.create_table(
            "user_branches",
            sa.Column("user_id", GUID(), nullable=False),
            sa.Column("tag_handle", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id", "tag_handle"),
        )

    if "succession_plans" not in tables:
        op.create_table(
            "succession_plans",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tree", sa.String(), nullable=False),
            sa.Column("user_id", GUID(), nullable=False),
            sa.Column("person_handle", sa.String(), nullable=True),
            sa.Column("required_confirmations", sa.Integer(), nullable=False),
            sa.Column("grace_days", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("death_recorded_at", sa.DateTime(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("execute_after", sa.DateTime(), nullable=True),
            sa.Column("executed_at", sa.DateTime(), nullable=True),
            sa.Column("executed_successor_id", GUID(), nullable=True),
            sa.Column("vetoed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_succession_plans_tree", "succession_plans", ["tree"])
        op.create_index("ix_succession_plans_user_id", "succession_plans", ["user_id"])

    if "succession_successors" not in tables:
        op.create_table(
            "succession_successors",
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("user_id", GUID(), nullable=False),
            sa.ForeignKeyConstraint(
                ["plan_id"], ["succession_plans.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("plan_id", "position"),
        )

    if "succession_confirmations" not in tables:
        op.create_table(
            "succession_confirmations",
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("user_id", GUID(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["plan_id"], ["succession_plans.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("plan_id", "user_id"),
        )


def downgrade():
    op.drop_table("succession_confirmations")
    op.drop_table("succession_successors")
    op.drop_index("ix_succession_plans_user_id", table_name="succession_plans")
    op.drop_index("ix_succession_plans_tree", table_name="succession_plans")
    op.drop_table("succession_plans")
    op.drop_table("user_branches")
