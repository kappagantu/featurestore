import os
from datetime import UTC, datetime

import requests
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .observability import configure_logging, instrument_fastapi, log_event
from .security import RequestContext, get_request_context, require_writer_or_admin

SERVICE = "lineage-service"
logger = configure_logging(SERVICE)
app = FastAPI(title="Lineage Service", version="0.3.0")
instrument_fastapi(app, SERVICE, logger)


class LineageDataset(BaseModel):
    namespace: str
    name: str


class LineageEvent(BaseModel):
    job_name: str
    run_id: str
    event_type: str = Field(pattern="^(START|COMPLETE|FAIL)$")
    inputs: list[LineageDataset] = Field(default_factory=list)
    outputs: list[LineageDataset] = Field(default_factory=list)


def _marquez_url() -> str:
    return os.getenv("MARQUEZ_URL", "http://localhost:5000")


def _namespace() -> str:
    return os.getenv("OPENLINEAGE_NAMESPACE", "featurestore.local")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "lineage-service"}


@app.get("/config")
def config() -> dict[str, str]:
    return {"marquez_url": _marquez_url(), "namespace": _namespace()}


@app.post("/lineage/events")
def emit_event(
    payload: LineageEvent,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, str]:
    require_writer_or_admin(ctx)
    namespace = f"{_namespace()}.{ctx.tenant_id}"
    event = {
        "eventType": payload.event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "run": {"runId": payload.run_id},
        "job": {"namespace": namespace, "name": payload.job_name},
        "inputs": [item.model_dump() for item in payload.inputs],
        "outputs": [item.model_dump() for item in payload.outputs],
        "producer": "feature-store/lineage-service",
        "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/definitions/RunEvent",
    }

    try:
        response = requests.post(
            f"{_marquez_url().rstrip('/')}/api/v1/lineage",
            json=event,
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send event to Marquez: {exc}") from exc

    log_event(
        logger,
        "lineage_event_emitted",
        tenant_id=ctx.tenant_id,
        run_id=payload.run_id,
        job_name=payload.job_name,
        event_type=payload.event_type,
    )

    return {
        "status": "accepted",
        "tenant_id": ctx.tenant_id,
        "job_name": payload.job_name,
        "run_id": payload.run_id,
        "event_type": payload.event_type,
    }
