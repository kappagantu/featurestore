from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import FeatureGroupModel, get_db
from .observability import configure_logging, instrument_fastapi
from .security import RequestContext, get_request_context, require_writer_or_admin

SERVICE = "feature-group-service"
logger = configure_logging(SERVICE)
app = FastAPI(title="Feature Group Service", version="0.3.0")
instrument_fastapi(app, SERVICE, logger)

SUPPORTED_SCHEMA_TYPES = {
    "string",
    "int",
    "float",
    "bool",
    "double",
    "array",
    "object",
}


class FeatureGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    entity: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    tags: list[str] = Field(default_factory=list)
    schema: dict = Field(default_factory=dict)


class FeatureGroupUpdate(BaseModel):
    entity: str | None = Field(default=None, min_length=1, max_length=128)
    owner: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    tags: list[str] | None = None
    schema: dict | None = None


class FeatureGroupResponse(BaseModel):
    tenant_id: str
    name: str
    entity: str
    owner: str
    description: str | None
    tags: list[str]
    schema: dict
    version: int


def _normalize_schema_type(value: Any) -> tuple[str, bool]:
    nullable = False
    raw_type: Any = value
    if isinstance(value, dict):
        raw_type = value.get("type")
        nullable = bool(value.get("nullable", False))

    if not isinstance(raw_type, str):
        raise HTTPException(status_code=400, detail="Schema types must be strings")

    schema_type = raw_type.strip().lower()
    if schema_type not in SUPPORTED_SCHEMA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported schema type '{raw_type}'. Supported types: {sorted(SUPPORTED_SCHEMA_TYPES)}",
        )
    return schema_type, nullable


def _schema_as_typed_map(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    typed: dict[str, dict[str, Any]] = {}
    for key, value in schema.items():
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=400, detail="Schema field names must be non-empty strings")
        schema_type, nullable = _normalize_schema_type(value)
        typed[key] = {"type": schema_type, "nullable": nullable}
    return typed


def _validate_schema_definition(schema: dict[str, Any]) -> dict[str, Any]:
    typed = _schema_as_typed_map(schema)
    normalized: dict[str, Any] = {}
    for key, spec in typed.items():
        if spec["nullable"]:
            normalized[key] = spec
        else:
            normalized[key] = spec["type"]
    return normalized


def _validate_schema_update(existing: dict[str, Any], updated: dict[str, Any]) -> None:
    current = _schema_as_typed_map(existing)
    new = _schema_as_typed_map(updated)

    removed = sorted(set(current) - set(new))
    if removed:
        raise HTTPException(
            status_code=400,
            detail=f"Schema evolution cannot remove existing fields: {removed}",
        )

    changed: list[str] = []
    for key in set(current).intersection(new):
        old = current[key]
        nxt = new[key]
        if old["type"] != nxt["type"]:
            changed.append(f"{key}: {old['type']} -> {nxt['type']}")
    if changed:
        raise HTTPException(
            status_code=400,
            detail=f"Schema evolution cannot change field types: {changed}",
        )


def _to_response(model: FeatureGroupModel) -> FeatureGroupResponse:
    return FeatureGroupResponse(
        tenant_id=model.tenant_id,
        name=model.name,
        entity=model.entity,
        owner=model.owner,
        description=model.description,
        tags=model.tags or [],
        schema=model.schema or {},
        version=model.version,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "feature-group-service"}


@app.post("/feature-groups", response_model=FeatureGroupResponse)
def create_feature_group(
    payload: FeatureGroupCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> FeatureGroupResponse:
    require_writer_or_admin(ctx)
    normalized_schema = _validate_schema_definition(payload.schema)
    existing = db.get(FeatureGroupModel, {"tenant_id": ctx.tenant_id, "name": payload.name})
    if existing:
        raise HTTPException(status_code=409, detail="Feature group already exists")

    model_data = payload.model_dump()
    model_data["schema"] = normalized_schema
    model = FeatureGroupModel(tenant_id=ctx.tenant_id, **model_data, version=1)
    db.add(model)
    db.commit()
    db.refresh(model)
    return _to_response(model)


@app.get("/feature-groups", response_model=list[FeatureGroupResponse])
def list_feature_groups(
    owner: str | None = Query(default=None),
    entity: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> list[FeatureGroupResponse]:
    stmt = select(FeatureGroupModel).where(FeatureGroupModel.tenant_id == ctx.tenant_id)
    if owner:
        stmt = stmt.where(FeatureGroupModel.owner == owner)
    if entity:
        stmt = stmt.where(FeatureGroupModel.entity == entity)
    rows = db.execute(stmt).scalars().all()
    return [_to_response(row) for row in rows]


@app.get("/feature-groups/{name}", response_model=FeatureGroupResponse)
def get_feature_group(
    name: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> FeatureGroupResponse:
    model = db.get(FeatureGroupModel, {"tenant_id": ctx.tenant_id, "name": name})
    if not model:
        raise HTTPException(status_code=404, detail="Feature group not found")
    return _to_response(model)


@app.put("/feature-groups/{name}", response_model=FeatureGroupResponse)
def update_feature_group(
    name: str,
    payload: FeatureGroupUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> FeatureGroupResponse:
    require_writer_or_admin(ctx)
    model = db.get(FeatureGroupModel, {"tenant_id": ctx.tenant_id, "name": name})
    if not model:
        raise HTTPException(status_code=404, detail="Feature group not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "schema" in update_data and update_data["schema"] is not None:
        _validate_schema_update(model.schema or {}, update_data["schema"])
        update_data["schema"] = _validate_schema_definition(update_data["schema"])
    for field, value in update_data.items():
        setattr(model, field, value)

    model.version += 1
    db.commit()
    db.refresh(model)
    return _to_response(model)


@app.delete("/feature-groups/{name}")
def delete_feature_group(
    name: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, str]:
    require_writer_or_admin(ctx)
    model = db.get(FeatureGroupModel, {"tenant_id": ctx.tenant_id, "name": name})
    if not model:
        raise HTTPException(status_code=404, detail="Feature group not found")

    db.delete(model)
    db.commit()
    return {"status": "deleted", "name": name}
