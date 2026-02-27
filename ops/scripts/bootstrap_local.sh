#!/usr/bin/env bash
set -euo pipefail

echo "Creating local S3 bucket and DynamoDB table in LocalStack..."
aws --endpoint-url=http://localhost:4566 s3 mb s3://features-offline || true
aws --endpoint-url=http://localhost:4566 dynamodb create-table \
  --table-name features_online \
  --attribute-definitions AttributeName=pk,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST || true
aws --endpoint-url=http://localhost:4566 dynamodb create-table \
  --table-name materialization_jobs \
  --attribute-definitions AttributeName=job_id,AttributeType=S \
  --key-schema AttributeName=job_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST || true

echo "Done."
