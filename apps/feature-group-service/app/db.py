import os
from collections.abc import Generator

from sqlalchemy import JSON, Index, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class FeatureGroupModel(Base):
    __tablename__ = "feature_groups"
    __table_args__ = (
        Index("ix_feature_groups_tenant_owner", "tenant_id", "owner"),
        Index("ix_feature_groups_tenant_entity", "tenant_id", "entity"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    schema: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(default=1)


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql+psycopg://fs:fs@localhost:5432/fs_meta")


engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
