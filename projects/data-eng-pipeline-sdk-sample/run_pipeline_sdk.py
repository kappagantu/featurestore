#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "packages/sdk/src"))

from sdk import FeatureGroupClient, FeatureStoreClient, LineageClient, OIDCTokenProvider  # noqa: E402


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "y"}


def _load_csv(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            records.append(
                {
                    "entity_id": row["entity_id"],
                    "event_ts": row.get("event_ts") or datetime.now(UTC).isoformat(),
                    "features": {
                        "avg_order_value_30d": float(row["avg_order_value_30d"]),
                        "orders_7d": int(row["orders_7d"]),
                        "is_high_value": _parse_bool(row["is_high_value"]),
                    },
                }
            )
    return records


def _load_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Parquet input requires pyarrow installed locally. "
            "Install with: pip install pyarrow"
        ) from exc

    table = pq.read_table(path)
    rows = table.to_pylist()
    records: list[dict[str, Any]] = []
    for row in rows:
        event_ts = row.get("event_ts")
        if hasattr(event_ts, "isoformat"):
            event_ts = event_ts.isoformat()
        records.append(
            {
                "entity_id": str(row["entity_id"]),
                "event_ts": event_ts or datetime.now(UTC).isoformat(),
                "features": {
                    "avg_order_value_30d": float(row["avg_order_value_30d"]),
                    "orders_7d": int(row["orders_7d"]),
                    "is_high_value": bool(row["is_high_value"]),
                },
            }
        )
    return records


def _load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path)
    if suffix == ".parquet":
        return _load_parquet(path)
    raise ValueError("Unsupported file type. Use .csv or .parquet")


def _print(name: str, payload: dict[str, Any]) -> None:
    print(f"\n== {name} ==")
    print(json.dumps(payload, indent=2, sort_keys=True))


def _wait_for_job(fs: FeatureStoreClient, job_id: str, timeout_sec: int = 120) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        status = fs.get_materialization_job(job_id)
        state = status.get("status")
        if state == "COMPLETE":
            return status
        if state == "FAILED":
            raise RuntimeError(f"Materialization job failed: {status.get('error')}")
        time.sleep(2)
    raise TimeoutError(f"Materialization job timed out: {job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SDK-based DE pipeline sample")
    parser.add_argument(
        "--input",
        default=str(Path(__file__).resolve().parent / "data/customer_behavior.csv"),
        help="Input file path (.csv or .parquet)",
    )
    parser.add_argument("--feature-group", default="customer_behavior_features_sdk")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    token_provider = None
    if os.getenv("FS_USE_OIDC", "false").strip().lower() == "true":
        token_provider = OIDCTokenProvider(
            token_url=os.getenv(
                "FS_OIDC_TOKEN_URL",
                "http://localhost:8088/realms/featurestore/protocol/openid-connect/token",
            ),
            client_id=os.getenv("FS_OIDC_CLIENT_ID", "featurestore-sdk"),
            client_secret=os.getenv("FS_OIDC_CLIENT_SECRET", "featurestore-secret"),
            scope=os.getenv("FS_OIDC_SCOPE"),
        )

    fg = FeatureGroupClient("http://localhost:8001", token_provider=token_provider)
    fs = FeatureStoreClient("http://localhost:8002", token_provider=token_provider)
    lineage = LineageClient("http://localhost:8003", token_provider=token_provider)

    records = _load_records(input_path)
    entity_ids = [item["entity_id"] for item in records]

    run_id = str(uuid.uuid4())
    job_name = "sample_pipeline_sdk.materialize_customer_behavior"

    start = lineage.emit_event(
        {
            "job_name": job_name,
            "run_id": run_id,
            "event_type": "START",
            "inputs": [{"namespace": "featurestore.local", "name": "raw.customer_behavior"}],
            "outputs": [{"namespace": "featurestore.local", "name": f"offline.{args.feature_group}"}],
        }
    )
    _print("lineage_start", start)

    try:
        fg_result = fg.create_feature_group(
            {
                "name": args.feature_group,
                "entity": "customer_id",
                "owner": "data-eng",
                "description": "SDK sample pipeline feature group",
                "tags": ["sdk", "sample", "batch"],
                "schema": {
                    "customer_id": "string",
                    "event_ts": "string",
                    "avg_order_value_30d": "float",
                    "orders_7d": "int",
                    "is_high_value": "bool",
                },
            }
        )
    except Exception:
        fg_result = fg.get_feature_group(args.feature_group)
    _print("feature_group", fg_result)

    ingest = fs.ingest_offline({"feature_group": args.feature_group, "records": records})
    _print("offline_ingest", ingest)

    queued = fs.materialize({"feature_group": args.feature_group, "entity_ids": entity_ids})
    _print("materialize_queued", queued)
    materialize = _wait_for_job(fs, queued["job_id"])
    _print("materialize_complete", materialize)

    online = fs.get_online({"feature_group": args.feature_group, "entity_id": entity_ids[0]})
    _print("online_get", online)

    complete = lineage.emit_event(
        {
            "job_name": job_name,
            "run_id": run_id,
            "event_type": "COMPLETE",
            "inputs": [{"namespace": "featurestore.local", "name": f"offline.{args.feature_group}"}],
            "outputs": [{"namespace": "featurestore.local", "name": f"online.{args.feature_group}"}],
        }
    )
    _print("lineage_complete", complete)

    print("\nSDK pipeline finished successfully.")
    print(f"Run ID: {run_id}")
    print(f"Input file: {input_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
