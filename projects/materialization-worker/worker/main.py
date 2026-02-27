import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from deltalake import DeltaTable

from .observability import (
    JOB_DURATION,
    JOBS_FAILED,
    JOBS_PROCESSED,
    configure_logging,
    configure_tracing,
    log_event,
    start_metrics_server,
)

SERVICE = "materialization-worker"
logger = configure_logging(SERVICE)
tracer = configure_tracing(SERVICE)


def _aws_region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def _aws_endpoint() -> str:
    return os.getenv("AWS_ENDPOINT_URL", "http://localstack:4566")


def _s3_bucket() -> str:
    return os.getenv("FEATURES_S3_BUCKET", "features-offline")


def _ddb_table() -> str:
    return os.getenv("FEATURES_DDB_TABLE", "features_online")


def _jobs_table() -> str:
    return os.getenv("MATERIALIZATION_JOBS_TABLE", "materialization_jobs")


def _poll_interval() -> int:
    return int(os.getenv("WORKER_POLL_INTERVAL_SEC", "5"))


def _max_jobs_per_poll() -> int:
    return int(os.getenv("WORKER_MAX_JOBS_PER_POLL", "5"))


def _ddb_resource():
    return boto3.resource(
        "dynamodb",
        endpoint_url=_aws_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_aws_endpoint(),
        region_name=_aws_region(),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _storage_options() -> dict[str, str]:
    return {
        "AWS_REGION": _aws_region(),
        "AWS_ENDPOINT_URL": _aws_endpoint(),
        "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", "test"),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        "AWS_ALLOW_HTTP": "true",
    }


def _delta_uri(tenant_id: str, feature_group: str) -> str:
    return f"s3://{_s3_bucket()}/delta/{tenant_id}/{feature_group}"


def _to_ddb_safe(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_to_ddb_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_ddb_safe(item) for key, item in value.items()}
    return value


def _ensure_resources() -> None:
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


def _read_offline_rows(tenant_id: str, feature_group: str, entity_ids: list[str]) -> list[dict[str, Any]]:
    table = DeltaTable(_delta_uri(tenant_id, feature_group), storage_options=_storage_options())

    filters = None
    if entity_ids:
        filters = [("entity_id", "in", entity_ids)]

    rows = table.to_pyarrow_table(filters=filters).to_pylist()

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


def _claim_job(jobs_table, job_id: str) -> bool:
    now = datetime.now(UTC).isoformat()
    try:
        jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :in_progress, updated_at = :u",
            ConditionExpression=Attr("status").eq("QUEUED"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":in_progress": "IN_PROGRESS", ":u": now},
        )
        return True
    except Exception:
        return False


def _complete_job(jobs_table, job_id: str, written: int) -> None:
    now = datetime.now(UTC).isoformat()
    jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :done, written = :w, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":done": "COMPLETE", ":w": written, ":u": now},
    )


def _fail_job(jobs_table, job_id: str, message: str) -> None:
    now = datetime.now(UTC).isoformat()
    jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :failed, #err = :e, updated_at = :u",
        ExpressionAttributeNames={"#s": "status", "#err": "error"},
        ExpressionAttributeValues={":failed": "FAILED", ":e": message[:500], ":u": now},
    )


def process_once() -> int:
    ddb = _ddb_resource()
    jobs = ddb.Table(_jobs_table())
    online = ddb.Table(_ddb_table())

    response = jobs.scan(
        FilterExpression=Attr("status").eq("QUEUED"),
        Limit=_max_jobs_per_poll(),
    )
    items = response.get("Items", [])

    processed = 0
    for item in items:
        job_id = item["job_id"]
        if not _claim_job(jobs, job_id):
            continue

        tenant_id = item["tenant_id"]
        feature_group = item["feature_group"]
        entity_ids = item.get("entity_ids") or []

        with tracer.start_as_current_span("materialization_job") as span:
            span.set_attribute("job.id", job_id)
            span.set_attribute("tenant.id", tenant_id)
            span.set_attribute("feature_group", feature_group)
            start = time.perf_counter()
            try:
                rows = _read_offline_rows(tenant_id, feature_group, entity_ids)
                written = 0
                with online.batch_writer(overwrite_by_pkeys=["pk"]) as batch:
                    for row in rows:
                        batch.put_item(
                            Item={
                                "pk": f"{tenant_id}#{feature_group}#{row['entity_id']}",
                                "tenant_id": tenant_id,
                                "feature_group": feature_group,
                                "entity_id": row["entity_id"],
                                "event_ts": row["event_ts"],
                                "features": _to_ddb_safe(row["features"]),
                            }
                        )
                        written += 1

                _complete_job(jobs, job_id, written)
                JOBS_PROCESSED.inc()
                JOB_DURATION.observe(time.perf_counter() - start)
                log_event(
                    logger,
                    "materialization_job_complete",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    feature_group=feature_group,
                    written=written,
                )
                processed += 1
            except Exception as exc:
                JOBS_FAILED.inc()
                try:
                    _fail_job(jobs, job_id, str(exc))
                except Exception as fail_exc:
                    log_event(
                        logger,
                        "materialization_job_fail_update_error",
                        job_id=job_id,
                        error=str(fail_exc),
                    )
                log_event(
                    logger,
                    "materialization_job_failed",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    feature_group=feature_group,
                    error=str(exc),
                )

    return processed


def main() -> None:
    start_metrics_server()
    log_event(logger, "worker_started", service=SERVICE)
    _ensure_resources()
    while True:
        try:
            count = process_once()
            if count:
                log_event(logger, "worker_batch_processed", count=count)
        except Exception as exc:
            log_event(logger, "worker_loop_error", error=str(exc))
        time.sleep(_poll_interval())


if __name__ == "__main__":
    main()
