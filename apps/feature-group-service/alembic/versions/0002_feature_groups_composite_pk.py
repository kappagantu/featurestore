"""make feature_groups primary key tenant-aware

Revision ID: 0002_feature_groups_composite_pk
Revises: 0001_create_feature_groups
Create Date: 2026-02-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_feature_groups_composite_pk"
down_revision: str | None = "0001_create_feature_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col["name"] for col in inspector.get_columns("feature_groups")}
    if "tenant_id" not in columns:
        op.add_column(
            "feature_groups",
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
        )

    op.execute("UPDATE feature_groups SET tenant_id = 'default' WHERE tenant_id IS NULL")
    op.alter_column(
        "feature_groups",
        "tenant_id",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="default",
    )

    pk = inspector.get_pk_constraint("feature_groups")
    pk_cols = pk.get("constrained_columns", [])
    pk_name = pk.get("name")
    if pk_cols != ["tenant_id", "name"]:
        if pk_name:
            op.drop_constraint(pk_name, "feature_groups", type_="primary")
        op.create_primary_key("feature_groups_pkey", "feature_groups", ["tenant_id", "name"])


def downgrade() -> None:
    pk = sa.inspect(op.get_bind()).get_pk_constraint("feature_groups")
    pk_name = pk.get("name")
    if pk_name:
        op.drop_constraint(pk_name, "feature_groups", type_="primary")
    op.create_primary_key("feature_groups_pkey", "feature_groups", ["name"])
