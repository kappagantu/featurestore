# Materialization Worker

Asynchronous data-plane worker for materialization jobs.

- Polls DynamoDB `materialization_jobs` for `QUEUED` jobs.
- Claims each job (`IN_PROGRESS`) with conditional update.
- Reads offline features from S3 Delta.
- Writes online rows to DynamoDB.
- Marks job `COMPLETE` (with `written`) or `FAILED` (with `error`).

This worker is intended to run separately from the API request path.
