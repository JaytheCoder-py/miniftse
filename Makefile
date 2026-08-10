.PHONY: help setup test test-fast lint format typecheck build-index factsheet \
        chaos-drill evals pin-golden check-golden daily docs docker clean all ci \
        desk-data desk-serve

UV ?= uv
SECURITIES ?= 300
START ?= 2016-01-04
END ?= 2026-06-30

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup:  ## Install dependencies and the package
	$(UV) sync --all-extras --dev

test:  ## Run the full test suite
	$(UV) run pytest tests/ -q

test-fast:  ## Unit and property tests only, skipping the slow integration tests
	$(UV) run pytest tests/test_index_maths.py tests/test_properties.py -q

coverage:  ## Run tests with a coverage report
	$(UV) run pytest tests/ -q --cov=miniftse --cov-report=term-missing

lint:  ## Lint
	$(UV) run ruff check src tests

format:  ## Format
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

typecheck:  ## Type-check under mypy --strict
	$(UV) run mypy src/miniftse

build-index:  ## Build the full index history
	$(UV) run miniftse build-index --securities $(SECURITIES) --start $(START) --end $(END)

factsheet:  ## Generate the client-facing factsheet
	$(UV) run miniftse factsheet --securities $(SECURITIES) --start $(START) --end $(END)

chaos-drill:  ## Inject data faults and report validation coverage
	$(UV) run miniftse chaos-drill --securities 150

evals:  ## Run the methodology assistant evaluation suite
	$(UV) run pytest tests/test_integration.py::TestAiLayer -q

pin-golden:  ## Re-pin the golden master (records a deliberate change to the index)
	$(UV) run miniftse pin-golden

check-golden:  ## Verify the current build against the pinned index history
	$(UV) run miniftse check-golden

daily:  ## Run the daily production DAG
	$(UV) run miniftse daily

daily-late-data:  ## Same, simulating a late market data file
	$(UV) run miniftse daily --simulate late_data

daily-blocked:  ## Same, simulating a price outlier that blocks publication
	$(UV) run miniftse daily --simulate outlier

desk-data:  ## Precompute every artefact the ops desk serves
	$(UV) run miniftse desk-snapshot

desk-serve:  ## Run the ops desk locally
	$(UV) run uvicorn miniftse.desk.app:app --reload --port 8000

docs:  ## Regenerate the documents that are generated from code
	$(UV) run miniftse sql-cookbook
	$(UV) run miniftse methodology

docker:  ## Build the container image
	docker build -t miniftse:local .

docker-run: docker  ## Build and run the container
	docker run --rm miniftse:local miniftse build-index --securities 100 \
		--start 2016-01-04 --end 2018-12-31

clean:  ## Remove build artefacts and caches
	rm -rf artefacts/ dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage \
		coverage.xml htmlcov/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

ci: lint typecheck test check-golden  ## Everything CI runs

all: setup ci build-index factsheet  ## Full pipeline from a clean clone
