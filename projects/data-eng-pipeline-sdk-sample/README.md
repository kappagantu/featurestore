# Data Engineering Pipeline SDK Sample

This sample uses the monorepo SDK (`packages/sdk`) instead of raw HTTP calls.

## What it does

1. Emits lineage START
2. Creates/reads feature group metadata
3. Ingests offline records from input file (CSV or Parquet)
4. Materializes to online DynamoDB store
5. Fetches one online entity
6. Emits lineage COMPLETE

## Run with CSV

```bash
make sample-run-sdk
```

## Run with OIDC token provider

Start stack with OIDC profile first:

```bash
make dev-up-oidc
```

Run SDK pipeline with automatic client-credentials token retrieval:

```bash
FS_USE_OIDC=true make sample-run-sdk
```

## Run with Parquet

```bash
python3 projects/data-eng-pipeline-sdk-sample/run_pipeline_sdk.py \
  --input /absolute/path/to/input.parquet
```

Note: Parquet input requires `pyarrow` installed on your local machine.
