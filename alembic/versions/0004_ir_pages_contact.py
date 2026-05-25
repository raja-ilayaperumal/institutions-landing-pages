"""Add office contact columns to ir_pages so we capture phone/fax/address
even when no individuals are published.

Revision ID: 0004_ir_pages_contact
Revises: 0003_landing_views
Create Date: 2026-05-24
"""
from alembic import op

revision = "0004_ir_pages_contact"
down_revision = "0003_landing_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE landing.ir_pages ADD COLUMN office_phone   TEXT")
    op.execute("ALTER TABLE landing.ir_pages ADD COLUMN office_fax     TEXT")
    op.execute("ALTER TABLE landing.ir_pages ADD COLUMN office_email   TEXT")
    op.execute("ALTER TABLE landing.ir_pages ADD COLUMN office_address TEXT")


def downgrade() -> None:
    for col in ("office_address", "office_email", "office_fax", "office_phone"):
        op.execute(f"ALTER TABLE landing.ir_pages DROP COLUMN IF EXISTS {col}")
