# Data Engineering Pipeline Sample

This project demonstrates an end-to-end data engineering workflow in the monorepo:

1. Create or fetch a feature group in Feature Group Service
2. Ingest batch feature records into S3 Delta via Feature Store Service
3. Materialize offline features into DynamoDB
4. Retrieve an online feature record
5. Emit START/COMPLETE lineage events to Marquez through Lineage Service

## Run

From repo root:

```bash
make sample-run
```

Direct run:

```bash
python3 projects/data-eng-pipeline-sample/run_pipeline.py
```

## Preconditions

- Stack is running (`make dev-up`)
- Services reachable on localhost ports 8001, 8002, 8003
