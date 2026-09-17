.PHONY: help install sync lint format format-check typecheck test test-cov test-integration \
	test-integration-local infra-up infra-down infra-logs audit check pre-commit clean

UV ?= uv
SRC := src
PKG := src/pycommon
TESTS := tests

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install all + dev extras (uv sync)
	$(UV) sync --extra all --extra dev

sync: install ## Alias for install

lint: ## Ruff lint (src + tests)
	$(UV) run ruff check $(SRC) $(TESTS)

format: ## Ruff format (write)
	$(UV) run ruff format $(SRC) $(TESTS)

format-check: ## Ruff format check (CI)
	$(UV) run ruff format --check $(SRC) $(TESTS)

typecheck: ## Mypy strict on package
	$(UV) run python -m mypy $(PKG)

test: ## Pytest
	$(UV) run python -m pytest

test-cov: ## Pytest with coverage
	$(UV) run python -m pytest --cov=pycommon --cov-report=term-missing

test-integration: ## Run integration tests against real Redis / Postgres (see CONTRIBUTING)
	@test -n "$$REDIS_TEST_URL" -o -n "$$POSTGRES_TEST_DSN" || { \
		echo "Neither REDIS_TEST_URL nor POSTGRES_TEST_DSN is set; everything would skip."; \
		echo "See the Testing section of CONTRIBUTING.md for the two containers."; \
		exit 1; }
	$(UV) run pytest tests/integration -v --no-cov

# The env every integration group reads, pointed at docker-compose.yaml. One
# definition so a contributor never assembles this by hand, and so the ports and
# credentials cannot drift from the compose file.
INFRA_ENV := \
	REDIS_TEST_URL=redis://localhost:6379/15 \
	POSTGRES_TEST_DSN=postgresql+asyncpg://pycommon:pycommon@localhost:5432/pycommon_test \
	OTLP_TEST_ENDPOINT=http://localhost:4317 \
	JAEGER_QUERY_URL=http://localhost:16686 \
	S3_TEST_ENDPOINT=http://localhost:9000 \
	S3_TEST_ACCESS_KEY=pycommon \
	S3_TEST_SECRET_KEY=pycommon123 \
	KEYCLOAK_TEST_URL=http://localhost:8080

infra-up: ## Start Redis/Postgres/Jaeger/MinIO/Keycloak for the integration suite, wait until healthy
	docker compose up -d --wait

infra-down: ## Stop them and delete their data
	docker compose down -v

infra-logs: ## Tail the service logs
	docker compose logs -f

test-integration-local: infra-up ## infra-up, then run the integration suite against it
	$(INFRA_ENV) $(UV) run pytest tests/integration -v --no-cov

keycloak-export: ## Dump the RUNNING Keycloak realm to /tmp, to diff against the fixture
	@# Answers "what did Keycloak make of what I wrote" -- the question worth
	@# asking when a claim does not come out as expected. Writes to /tmp on
	@# purpose: an export is over a thousand lines of defaults, and overwriting
	@# the fixture with it would trade a file a reviewer can read for one nobody
	@# will. Hand-merge what you need.
	@token=$$(curl -sf -X POST http://localhost:8080/realms/master/protocol/openid-connect/token \
		-d grant_type=password -d client_id=admin-cli -d username=admin -d password=admin \
		| python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') && \
	curl -sf -X POST -H "Authorization: Bearer $$token" \
		'http://localhost:8080/admin/realms/pycommon-test/partial-export?exportClients=true&exportGroupsAndRoles=true' \
		| python3 -m json.tool > /tmp/pycommon-keycloak-realm.json && \
	echo "wrote /tmp/pycommon-keycloak-realm.json ($$(wc -l < /tmp/pycommon-keycloak-realm.json) lines)"

audit: ## Audit locked dependencies for known vulnerabilities
	$(UV) export --frozen --extra all --no-dev --no-emit-project \
		--format requirements-txt -o /tmp/pycommon-requirements-audit.txt
	uvx pip-audit --requirement /tmp/pycommon-requirements-audit.txt --disable-pip

check: lint format-check typecheck test-cov ## Run full CI checks locally

pre-commit: ## Install + run pre-commit hooks
	$(UV) run pre-commit install
	$(UV) run pre-commit run --all-files

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find $(SRC) $(TESTS) -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find $(SRC) $(TESTS) -type f -name '*.py[co]' -delete 2>/dev/null || true
