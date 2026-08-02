# GROUP 10

# Multi-Gateway Payment Router & Automated Quality Gate

Practical exam project covering advanced unit testing, SQLite integration testing, secret handling, and GitHub Actions CI/CD for a Python payment routing engine.

## Project Overview

The `PaymentRouter` routes payment transactions through primary and backup gateways with retry, idempotency, and API-key security guardrails. This repository verifies that behavior using:

- **Phase 1:** Mock-based unit/security tests and on-disk SQLite integration tests
- **Phase 2:** GitHub Actions CI with Python matrix builds, secret injection, and coverage enforcement
- **Phase 3:** Live chaos demo (feature branch → PR → pipeline debug)

## Project Structure

```
sw_exam/
├── payment_router.py           # Application under test (exam baseline)
├── tests/
│   ├── test_unit.py            # Unit & security tests (mocks + monkeypatch)
│   └── test_integration.py     # Integration tests (on-disk SQLite + Mock Lie)
├── .github/workflows/ci.yml    # Production Quality Gate pipeline
├── pytest.ini                  # Pytest configuration
├── requirements.txt            # Test dependencies
```

## Setup

```bash
cd sw_exam
pip install -r requirements.txt
```

**Requirements:** Python 3.10+

Set a production-like API key locally (required by the security guardrail):

```bash
export PAYMENT_GATEWAY_API_KEY=PROD_TEST_SECRET_KEY_123
```

## Running Tests

```bash
# Full suite
pytest

# Stage 1 — Unit & security (< 1 second, mocked)
pytest tests/test_unit.py -v

# Stage 2 — Physical SQLite integration
pytest tests/test_integration.py -v

# Stage 3 — Coverage gate (≥ 90%)
pytest --cov=payment_router --cov-report=term-missing --cov-fail-under=90 tests/

# HTML test report + coverage report
pytest tests/ -v --html=report.html --self-contained-html \
  --cov=payment_router --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=90
open report.html
open htmlcov/index.html
```

## Test Coverage

| Layer | File | What it verifies |
|---|---|---|
| Unit | `test_unit.py` | Missing/default API key, amount & E.164 validation, idempotency, flaky primary retry + `time.sleep`, backup failover, dual HTTP 500 circuit break |
| Integration | `test_integration.py` | On-disk SQLite fixture teardown, back-to-back + concurrent `tx_id` races, Mock Lie (wrong SQL table) |

## CI Pipeline

On every push or pull request to `main`, GitHub Actions:

1. Runs on Python **3.10** and **3.11** in parallel
2. Injects `PAYMENT_GATEWAY_API_KEY` from repository secrets (fallback for local forks)
3. Executes unit tests, then integration tests
4. Enforces **≥ 90%** coverage via `--cov-fail-under=90`

## Branch Protection (manual GitHub setting)

On the GitHub repository, protect `main` with:

- Require a pull request before merging
- Require at least **1** approval
- Require status checks to pass (`quality-gate` for 3.10 and 3.11)
- Do not allow direct pushes to `main`

## Role Mapping (suggested)

| Role | Focus |
|---|---|
| Student A | Unit isolation, mocking, coverage |
| Student B | Pytest fixtures, SQLite, integration |
| Student C | GitHub Actions, matrix, branch protection |
| Student D | Chaos injection, secrets, failure tests |
