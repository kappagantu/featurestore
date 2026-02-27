.PHONY: up down logs tree fmt dev-up dev-up-oidc dev-down ps sample-run sample-run-sdk

dev-up:
	docker compose -f infra/docker/docker-compose.yml up -d --build

dev-up-oidc:
	AUTH_ENABLED=true AUTH_MODE=oidc docker compose -f infra/docker/docker-compose.yml --profile oidc up -d --build

dev-down:
	docker compose -f infra/docker/docker-compose.yml down -v

up: dev-up

down: dev-down

logs:
	docker compose -f infra/docker/docker-compose.yml logs -f

ps:
	docker compose -f infra/docker/docker-compose.yml ps

sample-run:
	python3 projects/data-eng-pipeline-sample/run_pipeline.py

sample-run-sdk:
	python3 projects/data-eng-pipeline-sdk-sample/run_pipeline_sdk.py

tree:
	find . -maxdepth 3 -type d | sort

fmt:
	@echo "Run formatter per service (ruff/black) once dependencies are installed."
