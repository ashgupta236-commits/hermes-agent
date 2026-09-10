# cogos — persistent cognitive OS around Claude (see CLAUDE.md, docs/cogos/)
PY ?= .venv/bin/python

.PHONY: cogos-setup cogos-test cogos-lint cogos-typecheck cogos-eval cogos-demo cogos-check cogos-boot

cogos-setup:            ## create the venv with dev extras (uv)
	uv sync --extra dev

cogos-test:             ## unit + scenario tests for the cogos package
	$(PY) -m pytest tests/cogos -q

cogos-lint:             ## ruff over the cogos package and its tests
	.venv/bin/ruff check cogos tests/cogos

cogos-typecheck:        ## ty type check over the cogos package
	.venv/bin/ty check cogos

cogos-eval:             ## acceptance (A–J) + adversarial evaluation suite
	$(PY) -m cogos eval --suite all --write .cogos/eval-report.json

cogos-demo:             ## minimal autonomous demonstration (offline, scripted executive)
	$(PY) -m cogos demo

cogos-boot:             ## boot/recovery report
	$(PY) -m cogos boot

cogos-check: cogos-test cogos-lint cogos-typecheck cogos-eval cogos-demo   ## everything
