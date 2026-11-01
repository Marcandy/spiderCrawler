.PHONY: install lint format test cov run seed

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
