.PHONY: install format lint typecheck test build check

install:
	uv sync --dev

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy src

test:
	uv run pytest --cov=ruleloom --cov-report=term-missing

build:
	uv build --no-sources
	uv run --frozen twine check --strict dist/*.whl dist/*.tar.gz

check: lint typecheck test build
