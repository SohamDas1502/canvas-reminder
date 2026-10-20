VENV := .venv
BIN := $(VENV)/bin
REGION := ap-southeast-2

# Personal deploy settings (email addresses) live in the git-ignored deploy.env.
-include deploy.env
REQUIRE_ENV = @test -n "$(ALARM_EMAIL)" || (echo "Create deploy.env from deploy.env.example first" && exit 1)

.PHONY: help setup lint format typecheck test check validate build local check-feed demo configure localstack local-real invoke deploy pause resume web-validate web-build web-deploy web-publish web-dev clean

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:  ## Create the virtualenv, install dev dependencies and git hooks
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(BIN)/python -r requirements-dev.txt
	pre-commit install

lint:  ## Ruff lint + format check
	$(BIN)/ruff check src tests scripts
	$(BIN)/ruff format --check src tests scripts

format:  ## Auto-format
	$(BIN)/ruff check --fix src tests scripts
	$(BIN)/ruff format src tests scripts

typecheck:  ## mypy (strict) on the Lambda package
	$(BIN)/mypy src/canvas_reminder

test:  ## Unit + integration tests with coverage
	$(BIN)/pytest

check: lint typecheck test validate  ## Everything CI runs (except sam build)

validate:  ## Validate and lint the SAM template
	sam validate --lint --region $(REGION)

build:  ## Build the Lambda package
	sam build

# The sample feed's dates are fixed, so dry runs pretend it is 20 Oct 2026.
DEMO := $(BIN)/python scripts/run_local.py --dry-run --now 2026-10-20T00:00:00+00:00
SAMPLE := tests/fixtures/sample.ics

local:  ## Dry run: sample feed twice (2nd sends nothing), then with a due date changed
	$(DEMO) --ics $(SAMPLE) --ics $(SAMPLE) --ics tests/fixtures/sample_due_changed.ics

check-feed:  ## Paste your real feed URL (hidden): shows what you'd be sent. No AWS, nothing sent or saved
	$(BIN)/python scripts/run_local.py --dry-run --ask-url

configure:  ## Choose reminder times and apps (writes config.yaml, can deploy)
	$(BIN)/python scripts/configure.py

demo:  ## Local web demo at http://127.0.0.1:8000 (real pipeline, fake feed, nothing sent)
	$(BIN)/python scripts/demo_server.py

localstack:  ## Start LocalStack (DynamoDB) for real local runs
	docker compose up -d localstack

local-real:  ## Real run: secrets from .env, state in LocalStack, real notifications
	$(BIN)/python scripts/run_local.py --create-table

invoke: build  ## sam local invoke (needs Docker, LocalStack and env.local.json)
	sam local invoke ReminderFunction --event events/scheduled.json --env-vars env.local.json

deploy: build  ## Deploy the personal stack
	$(REQUIRE_ENV)
	sam deploy --parameter-overrides AlarmEmail=$(ALARM_EMAIL)

pause:  ## Stop everything that costs money (what the budget kill switch does)
	$(BIN)/python scripts/pause_resume.py pause

resume:  ## Undo a pause or the budget kill switch
	$(BIN)/python scripts/pause_resume.py resume

web-validate:  ## Validate the multi-user web stack template
	cd web && sam validate --lint --region $(REGION)

web-build:  ## Build the multi-user web stack
	cd web && sam build

web-deploy: web-build  ## Deploy the web stack, then publish the site
	$(REQUIRE_ENV)
	cd web && sam deploy --parameter-overrides AlarmEmail=$(ALARM_EMAIL) FromEmail=$(FROM_EMAIL)
	$(BIN)/python scripts/web_publish.py

web-publish:  ## Upload the website to S3 and refresh CloudFront
	$(BIN)/python scripts/web_publish.py

web-dev:  ## Website locally on http://127.0.0.1:8100 (fake sign-in code 123456, sample feed, nothing sent)
	$(BIN)/python scripts/web_dev.py

clean:
	rm -rf .aws-sam web/.aws-sam .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
