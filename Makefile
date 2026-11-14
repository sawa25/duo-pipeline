PY ?= /home/sawa/anaconda3/envs/env311/bin/python

.PHONY: test lint check

test:
	$(PY) -m pytest tests

lint:
	$(PY) -m ruff check .

check: lint test
