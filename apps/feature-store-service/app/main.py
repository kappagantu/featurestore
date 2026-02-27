import json
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib import error, request

import boto3
from botocore.exceptions import ClientError
from deltalake import DeltaTable, write_deltalake
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import pyarrow as pa

from .observability import configure_logging, instrument_fastapi, log_event
from .security import RequestContext, get_request_context, require_writer_or_admin

SERVICE = "feature-store-service"
logger = configure_logging(SERVICE)
app = FastAPI(title="Feature Store Service", version="0.4.0")
instrument_fastapi(app, SERVICE, logger)


class OfflineIngestRecord(BaseModel):
    entity_id: str
    event_ts: str
    features: dict[str, Any]


class OfflineIngestRequest(BaseModel):
    feature_group: str
    records: list[OfflineIngestRecord] = Field(min_length=1)


class MaterializeRequest(BaseModel):
    feature_group: str
    entity_ids: list[str] | None = None


class OnlineGetRequest(BaseModel):
    feature_group: str
    entity_id: str


def _aws_region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def _aws_endpoint() -> str:
    return os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")


def _s3_bucket() -> str:
    return os.getenv("FEATURES_S3_BUCKET", "features-offline")


def _ddb_table() -> str:
    return os.getenv("FEATURES_DDB_TABLE", "features_online")


def _jobs_table() -> str:
    return os.getenv("MATERIALIZATION_JOBS_TABLE", "materialization_jobs")


def _feature_group_service_url() -> str:
    return os.getenv("FEATURE_GROUP_SERVICE_URL", "http://feature-group-service:8001")


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_aws_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _ddb_resource():
    return boto3.resource(
        "dynamodb",
        endpoint_url=_aws_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _delta_uri(tenant_id: str, feature_group: str) -> str:
    return f"s3://{_s3_bucket()}/delta/{tenant_id}/{feature_group}"


def _storage_options() -> dict[str, str]:
    return {
        "AWS_REGION": _aws_region(),
        "AWS_ENDPOINT_URL": _aws_endpoint(),
        "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", "test"),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        "AWS_ALLOW_HTTP": "true",
    }


def _ensure_bucket_and_tables() -> None:
    s3 = _s3_client()
    ddb = _ddb_resource()

    try:
        s3.head_bucket(Bucket=_s3_bucket())
    except ClientError:
        s3.create_bucket(Bucket=_s3_bucket())

    existing = {table.name for table in ddb.tables.all()}

    if _ddb_table() not in existing:
        ddb.create_table(
            TableName=_ddb_table(),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    if _jobs_table() not in existing:
        ddb.create_table(
            TableName=_jobs_table(),
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def _records_to_arrow(
    records: list[OfflineIngestRecord], tenant_id: str, feature_group: str
) -> pa.Table:
    rows = []
    for rec in records:
        rows.append(
            {
                "tenant_id": tenant_id,
                "feature_group": feature_group,
                "entity_id": rec.entity_id,
                "event_ts": rec.event_ts,
                "features_json": json.dumps(rec.features),
            }
        )
    return pa.Table.from_pylist(rows)


def _query_offline(
    tenant_id: str,
    feature_group: str,
    entity_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        table = DeltaTable(_delta_uri(tenant_id, feature_group), storage_options=_storage_options())
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Offline table not found: {exc}") from exc

    filters = None
    if entity_ids:
        values = list(entity_ids)
        if values:
            filters = [("entity_id", "in", values)]

    arrow_table = table.to_pyarrow_table(filters=filters)
    rows = arrow_table.to_pylist()

    parsed: list[dict[str, Any]] = []
    for row in rows:
        parsed.append(
            {
                "tenant_id": row.get("tenant_id", tenant_id),
                "feature_group": row["feature_group"],
                "entity_id": row["entity_id"],
                "event_ts": row["event_ts"],
                "features": json.loads(row["features_json"]),
            }
        )
    return parsed


def _normalize_schema_type(value: Any) -> tuple[str, bool]:
    nullable = False
    raw_type = value
    if isinstance(value, dict):
        raw_type = value.get("type")
        nullable = bool(value.get("nullable", False))
    if not isinstance(raw_type, str):
        raise HTTPException(status_code=502, detail="Feature group schema is invalid: type must be a string")

    schema_type = raw_type.strip().lower()
    supported = {"string", "int", "float", "bool", "double", "array", "object"}
    if schema_type not in supported:
        raise HTTPException(
            status_code=502,
            detail=f"Feature group schema has unsupported type '{raw_type}'",
        )
    return schema_type, nullable


def _is_type_compatible(expected_type: str, value: Any) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type in {"float", "double"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "bool":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return False


def _load_feature_group(
    tenant_id: str,
    user_id: str,
    role: str,
    feature_group: str,
    authorization: str | None,
) -> dict[str, Any]:
    url = f"{_feature_group_service_url().rstrip('/')}/feature-groups/{feature_group}"
    headers = {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id,
        "X-Role": role,
    }
    if authorization:
        headers["Authorization"] = authorization
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if exc.code == 404:
            raise HTTPException(status_code=404, detail=f"Feature group '{feature_group}' not found") from exc
        raise HTTPException(
            status_code=502,
            detail=f"Failed to read feature group schema: {exc.code} {body}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to read feature group schema: {exc}") from exc


def _validate_records_against_schema(records: list[OfflineIngestRecord], fg: dict[str, Any]) -> None:
    schema = fg.get("schema") or {}
    if not isinstance(schema, dict) or not schema:
        return

    entity_field = str(fg.get("entity") or "").strip()
    excluded_fields = {"entity_id", "event_ts"}
    if entity_field:
        excluded_fields.add(entity_field)

    normalized_schema: dict[str, tuple[str, bool]] = {}
    for key, spec in schema.items():
        if key in excluded_fields:
            continue
        normalized_schema[key] = _normalize_schema_type(spec)

    expected_keys = set(normalized_schema.keys())
    for idx, record in enumerate(records, start=1):
        feature_keys = set(record.features.keys())
        missing = sorted(expected_keys - feature_keys)
        extra = sorted(feature_keys - expected_keys)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Record {idx} missing feature fields required by schema: {missing}",
            )
        if extra:
            raise HTTPException(
                status_code=400,
                detail=f"Record {idx} has fields not defined in schema: {extra}",
            )

        for field, (expected_type, nullable) in normalized_schema.items():
            value = record.features.get(field)
            if value is None:
                if nullable:
                    continue
                raise HTTPException(
                    status_code=400,
                    detail=f"Record {idx} field '{field}' is null but schema requires non-null {expected_type}",
                )
            if not _is_type_compatible(expected_type, value):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Record {idx} field '{field}' has type {type(value).__name__}; "
                        f"expected {expected_type}"
                    ),
                )


@app.on_event("startup")
def startup() -> None:
    _ensure_bucket_and_tables()


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "feature-store-service",
        "offline_store": "s3-delta",
        "online_store": "dynamodb",
        "materialization_mode": "async-worker",
    }


@app.get("/config")
def config() -> dict[str, str]:
    return {
        "s3_bucket": _s3_bucket(),
        "dynamodb_table": _ddb_table(),
        "jobs_table": _jobs_table(),
        "aws_endpoint_url": _aws_endpoint(),
    }


@app.post("/offline/ingest")
def offline_ingest(
    payload: OfflineIngestRequest,
    ctx: RequestContext = Depends(get_request_context),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_writer_or_admin(ctx)
    fg = _load_feature_group(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        role=ctx.role,
        feature_group=payload.feature_group,
        authorization=authorization,
    )
    _validate_records_against_schema(payload.records, fg)
    table = _records_to_arrow(payload.records, ctx.tenant_id, payload.feature_group)
    uri = _delta_uri(ctx.tenant_id, payload.feature_group)

    write_deltalake(
        uri,
        table,
        mode="append",
        partition_by=["tenant_id", "feature_group"],
        storage_options=_storage_options(),
    )

    return {
        "status": "ingested",
        "tenant_id": ctx.tenant_id,
        "rows": len(payload.records),
        "delta_uri": uri,
    }


@app.get("/offline/features/{feature_group}")
def offline_get(
    feature_group: str,
    entity_id: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    ids = [entity_id] if entity_id else None
    rows = _query_offline(ctx.tenant_id, feature_group, ids)
    return {
        "tenant_id": ctx.tenant_id,
        "feature_group": feature_group,
        "count": len(rows),
        "rows": rows,
    }


@app.post("/materialize", status_code=202)
def materialize(
    payload: MaterializeRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    require_writer_or_admin(ctx)
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    item = {
        "job_id": job_id,
        "tenant_id": ctx.tenant_id,
        "feature_group": payload.feature_group,
        "entity_ids": payload.entity_ids or [],
        "status": "QUEUED",
        "created_at": now,
        "updated_at": now,
        "requested_by": ctx.user_id,
    }

    _ddb_resource().Table(_jobs_table()).put_item(Item=item)
    log_event(
        logger,
        "materialization_job_queued",
        tenant_id=ctx.tenant_id,
        job_id=job_id,
        feature_group=payload.feature_group,
        requested_by=ctx.user_id,
    )

    return {
        "status": "queued",
        "job_id": job_id,
        "tenant_id": ctx.tenant_id,
        "feature_group": payload.feature_group,
        "jobs_table": _jobs_table(),
    }


@app.get("/materialize/jobs/{job_id}")
def materialize_job_status(
    job_id: str,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    response = _ddb_resource().Table(_jobs_table()).get_item(Key={"job_id": job_id})
    item = response.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Materialization job not found")
    if item.get("tenant_id") != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="Materialization job not found")
    return item


@app.post("/online/get")
def online_get(
    payload: OnlineGetRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    key = f"{ctx.tenant_id}#{payload.feature_group}#{payload.entity_id}"
    response = _ddb_resource().Table(_ddb_table()).get_item(Key={"pk": key})
    item = response.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Online features not found")
    return item
