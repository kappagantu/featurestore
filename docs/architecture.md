# Architecture

## Core Components

1. Feature Group Service (FastAPI + PostgreSQL)
2. Feature Store Service (FastAPI + S3 Delta + DynamoDB)
3. Lineage Service (FastAPI + OpenLineage -> Marquez)
4. Graph UI (Marquez Web in phase-1)

## Data Flow

- Producers ingest features to offline store (S3 Delta).
- Materialization jobs move selected features to DynamoDB.
- Service reads online features from DynamoDB for low-latency inference.
- Pipelines emit OpenLineage events to Marquez.
- Marquez UI renders lineage DAG.
