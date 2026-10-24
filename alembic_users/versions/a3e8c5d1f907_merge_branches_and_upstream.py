"""Merge the branches and succession tables with the upstream migrations

Both lines start at d4e9a1c7b3f2: this fork's 7b3f9c2a4d10 (branches,
succession) and upstream's access tokens, push subscriptions and task
lifecycle. A database at 7b3f9c2a4d10 gets the upstream ones on upgrade.

Revision ID: a3e8c5d1f907
Revises: 7b3f9c2a4d10, 9c4d2e7a5b18
Create Date: 2026-10-24 00:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "a3e8c5d1f907"
down_revision = ("7b3f9c2a4d10", "9c4d2e7a5b18")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
