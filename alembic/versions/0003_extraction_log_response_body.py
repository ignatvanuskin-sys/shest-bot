"""add extraction_logs.response_body (ТЗ §11 «полные тела ответов LLM на DEBUG»)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15

Backward compatible both ways:

* ``upgrade`` only *adds* a nullable column — old rows keep ``NULL`` ("the body was
  not recorded"), no data is rewritten and no default is needed, so it is a plain
  ``ALTER TABLE ADD COLUMN`` on SQLite (and on any other backend). The retention
  pass (``LeadService.purge_extraction_logs``) works on existing rows unchanged:
  it deletes by ``created_at``, not by the new column.
* ``downgrade`` drops it again. SQLite cannot ``ALTER TABLE DROP COLUMN`` on every
  version, so it goes through ``batch_alter_table`` (table recreate) — which on
  other backends degrades to a plain ``DROP COLUMN``.

Truncation and retention are application-level rules (see
``app/services/extraction.py`` and ``app/services/background.py``): the column is
plain ``TEXT`` because SQLite ignores a length limit anyway — enforcing the 4000
character cap in the writer is what actually holds.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extraction_logs", sa.Column("response_body", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("extraction_logs") as batch:
        batch.drop_column("response_body")
