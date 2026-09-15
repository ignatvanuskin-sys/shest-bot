"""add leads.phone_raw (ТЗ §7 «исходное написание для аудита»)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15

Backward compatible both ways:

* ``upgrade`` only *adds* a nullable column — existing rows keep ``NULL``, no data
  is rewritten and no default is needed, so it is a plain ``ALTER TABLE ADD COLUMN``
  on SQLite (and on any other backend).
* ``downgrade`` drops it again. SQLite cannot ``ALTER TABLE DROP COLUMN`` on every
  version, so it goes through ``batch_alter_table`` (table recreate) — which on
  other backends degrades to a plain ``DROP COLUMN``.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("phone_raw", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("phone_raw")
