"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("whatsapp_number", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("instagram", sa.Text(), nullable=True),
        sa.Column("telegram", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("services", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("reviews_count", sa.Integer(), nullable=True),
        sa.Column("contact_person", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="новый"),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("pain_point", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("last_action", sa.Text(), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sheet_row", sa.Integer(), nullable=True),
        sa.Column("duplicate_of_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "lead_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False, index=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="collecting"),
        sa.Column("combined_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("resulting_lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True),
    )

    op.create_table(
        "raw_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("lead_sessions.id"), nullable=True, index=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=True, index=True),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "extraction_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("lead_sessions.id"), nullable=True, index=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd_est", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
        sa.Column("added_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.Integer(), nullable=True, index=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=True, index=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("users")
    op.drop_table("extraction_logs")
    op.drop_table("raw_messages")
    op.drop_table("lead_sessions")
    op.drop_table("leads")
