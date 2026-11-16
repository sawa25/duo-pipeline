# Интерпретатор: по умолчанию python3 из PATH (на машине — активное окружение с ruff и pytest);
# иное — `make check PY=/путь/к/python`. Нужен Python >= 3.11.
PY ?= python3

.PHONY: test lint check

test:
	$(PY) -m pytest tests

lint:
	$(PY) -m ruff check .

check: lint test
