PYTHONS ?= 3.11 3.12 3.13 3.14

# pydantic v1 needs a FastAPI release that still supports it, and an older httpx
# for starlette's TestClient. Kept in sync with .github/workflows/ci.yml.
V1_PINS = --with 'pydantic<2' --with 'fastapi==0.111.0' --with 'httpx<0.28'

.PHONY: test test-all test-v1 lint help

help:
	@echo "make test       - run tests on the current interpreter (pydantic v2)"
	@echo "make test-all   - full matrix: each Python ($(PYTHONS)) x pydantic v1/v2"
	@echo "make test-v1    - current interpreter, pydantic v1 (pinned FastAPI/httpx)"

test:
	uv run --all-extras pytest -q

test-v1:
	uv run --all-extras $(V1_PINS) pytest -q

test-all:
	@for v in $(PYTHONS); do \
	  echo "===== py$$v · pydantic v2 ====="; \
	  uv run --python $$v --all-extras pytest -q || exit 1; \
	  echo "===== py$$v · pydantic v1 ====="; \
	  uv run --python $$v --all-extras $(V1_PINS) pytest -q || exit 1; \
	done
	@echo "All matrix combinations passed."
