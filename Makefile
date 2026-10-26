PY ?= python
export PYTHONPATH := src:tools:tests/architecture:tests/platform

.PHONY: check lint types arch test fix
check: lint types arch test

lint:
	ruff format --check . && ruff check .
types:
	mypy src tools tests/architecture tests/shared
	mypy src tests/platform  # separate run: two conftest.py files cannot share one mypy run
	mypy apps
arch:
	$(PY) -m archguard check
	lint-imports
	$(PY) -m archguard gen-importlinter --check
	$(PY) -m archguard api-lock
test:
	pytest -q
fix:
	ruff format . && ruff check --fix .
