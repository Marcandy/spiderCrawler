.PHONY: install lint format test cov run seed eval eval-live eval-test

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

cov:
	uv run pytest --cov=spidercrawler --cov-report=term-missing

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
