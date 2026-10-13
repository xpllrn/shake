.PHONY: help install test lint fmt typecheck check build clean

SHELL := /bin/bash
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
UV ?= uv

help:
	@echo "Available commands:"
	@echo "  make install    - Install shake in editable mode with dev dependencies"
	@echo "  make test       - Run test suite with pytest"
	@echo "  make lint       - Run ruff linter"
	@echo "  make fmt        - Run ruff code formatter"
	@echo "  make typecheck  - Run mypy type checker"
	@echo "  make check      - Run lint, typecheck, and test suite"
	@echo "  make build      - Build wheel and source distribution"
	@echo "  make clean      - Remove build artifacts, pycache, and caches"

install:
	$(UV) pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -v

lint:
	$(PYTHON) -m ruff check .

fmt:
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m mypy src tests

check: lint typecheck test

build:
	$(UV) build

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
