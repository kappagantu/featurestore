#!/usr/bin/env python3
"""Sample DE pipeline for the feature-store monorepo.

Flow:
1) Ensure feature group exists
2) Ingest sample features into offline S3 Delta
3) Materialize features into DynamoDB
4) Fetch one entity from online store
5) Emit lineage START and COMPLETE events
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib import error, request

FG_URL = "http://localhost:8001"
FS_URL = "http://localhost:8002"
LIN_URL = "http://localhost:8003"


class HttpError(RuntimeError):
    pass


def http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-Id": os.getenv("FS_TENANT_ID", "dev-tenant"),
        "X-User-Id": os.getenv("FS_USER_ID", "sample-user"),
        "X-Role": os.getenv("FS_ROLE", "admin"),
    }
    token = os.getenv("FS_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8")
        raise HttpError(f"{method} {url} -> {exc.code}: {details}") from exc
    except error.URLError as exc:
        raise HttpError(f"{method} {url} failed: {exc}") from exc


def print_step(name: str, payload: dict[str, Any]) -> None:
    print(f"\n== {name} ==")
    print(json.dumps(payload, indent=2, sort_keys=True))


def wait_for_job(job_id: str, timeout_sec: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        result = http_json("GET", f"{FS_URL}/materialize/jobs/{job_id}")
        state = result.get("status")
        if state == "COMPLETE":
            return result
        if state == "FAILED":
            raise HttpError(f"Materialization failed: {result.get('error')}")
        time.sleep(2)
    raise HttpError(f"Materialization job timed out: {job_id}")


def main() -> int:
    feature_group = "customer_behavior_features"
    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    inputs = [{"namespace": "featurestore.local", "name": "raw.customer_events"}]
    outputs = [{"namespace": "featurestore.local", "name": f"offline.{feature_group}"}]

    try:
        start_event = http_json(
            "POST",
            f"{LIN_URL}/lineage/events",
            {
                "job_name": "sample_pipeline.materialize_customer_behavior",
                "run_id": run_id,
                "event_type": "START",
                "inputs": inputs,
                "outputs": outputs,
            },
        )
        print_step("lineage_start", start_event)

        fg_payload = {
            "name": feature_group,
            "entity": "customer_id",
            "owner": "data-eng",
            "description": "Sample feature group for DE pipeline demo",
            "tags": ["sample", "demo", "batch"],
            "schema": {
                "customer_id": "string",
                "event_ts": "string",
                "avg_order_value_30d": "float",
                "orders_7d": "int",
                "is_high_value": "bool",
            },
        }
        try:
            fg_result = http_json("POST", f"{FG_URL}/feature-groups", fg_payload)
        except HttpError as exc:
            if "409" not in str(exc):
                raise
            fg_result = http_json("GET", f"{FG_URL}/feature-groups/{feature_group}")
        print_step("feature_group", fg_result)

        ingest_payload = {
            "feature_group": feature_group,
            "records": [
                {
                    "entity_id": "cust-001",
                    "event_ts": now,
                    "features": {
                        "avg_order_value_30d": 122.5,
                        "orders_7d": 3,
                        "is_high_value": True,
                    },
                },
                {
                    "entity_id": "cust-002",
                    "event_ts": now,
                    "features": {
                        "avg_order_value_30d": 45.2,
                        "orders_7d": 1,
                        "is_high_value": False,
                    },
                },
            ],
        }
        ingest_result = http_json("POST", f"{FS_URL}/offline/ingest", ingest_payload)
        print_step("offline_ingest", ingest_result)

        materialize_queued = http_json(
            "POST",
            f"{FS_URL}/materialize",
            {"feature_group": feature_group, "entity_ids": ["cust-001", "cust-002"]},
        )
        print_step("materialize_queued", materialize_queued)
        materialize_result = wait_for_job(materialize_queued["job_id"])
        print_step("materialize_complete", materialize_result)

        online_result = http_json(
            "POST",
            f"{FS_URL}/online/get",
            {"feature_group": feature_group, "entity_id": "cust-001"},
        )
        print_step("online_get", online_result)

        complete_event = http_json(
            "POST",
            f"{LIN_URL}/lineage/events",
            {
                "job_name": "sample_pipeline.materialize_customer_behavior",
                "run_id": run_id,
                "event_type": "COMPLETE",
                "inputs": inputs,
                "outputs": [
                    {"namespace": "featurestore.local", "name": f"online.{feature_group}"}
                ],
            },
        )
        print_step("lineage_complete", complete_event)

        print("\nPipeline run finished successfully.")
        print(f"Run ID: {run_id}")
        return 0
    except HttpError as exc:
        print(f"\nPipeline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
