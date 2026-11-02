# Where records live. After `make data-checkout`, use DATA_DIR=data-branch/data.
DATA_DIR ?= data

.PHONY: install lint format test cov run seed eval eval-live eval-test review digest data-checkout data-push publish

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest

# CI fails if coverage of src/spidercrawler drops below this floor (92% when set).
cov:
	uv run pytest --cov=spidercrawler --cov-report=term-missing --cov-fail-under=90

run:
	uv run spidercrawler run

seed:
	uv run spidercrawler run --seed

eval:
	uv run spidercrawler eval --split dev

eval-live:
	uv run spidercrawler eval --split dev --live

# The frozen test split. Scoring it is counted and shown in every test report.
eval-test:
	uv run spidercrawler eval --split test

# uvicorn is pulled in for this command only (not yet a project dependency).
review:
	uv run --with uvicorn spidercrawler review --data-dir $(DATA_DIR)

digest:
	uv run spidercrawler digest --data-dir $(DATA_DIR)

# Review workflow (PLAN.md section 7): pull the data branch, review, push decisions, publish.
data-checkout:
	@if [ -d data-branch ]; then git -C data-branch pull --ff-only; \
	else git fetch origin data && git worktree add data-branch data; fi

data-push:
	git -C data-branch add data/reviews
	git -C data-branch commit -m "Review decisions"
	git -C data-branch push origin data

publish:
	gh workflow run pages.yml
