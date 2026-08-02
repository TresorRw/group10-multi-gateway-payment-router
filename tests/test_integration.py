"""Physical SQLite integration tests for PaymentRouter.

================================================================================
THE 'MOCK LIE' — Why Unit Tests Pass While Integration Tests Catch SQL Breakage
================================================================================

Unit tests replace DatabaseRepository with unittest.mock.Mock objects. A Mock
accepts ANY method call and returns whatever we configure — it never executes
real SQL. Therefore a typo like:

    INSERT INTO tx_history ...   # wrong table; real schema uses `transactions`

is invisible to the unit suite: Mock.record_transaction(...) "succeeds" and
assertions on return codes (COMPLETED_PRIMARY, etc.) still pass. Coverage can
even report 100% on payment_router.py because every branch was exercised
against fakes.

Integration tests open a real on-disk SQLite file, CREATE TABLE transactions,
and run the same business path. Invalid SQL raises sqlite3.OperationalError
("no such table: tx_history"). That is the written proof that mocking the DB
can lie about persistence correctness — only a physical schema check catches it.

See test_mock_lie_invalid_sql_fails_against_real_database below.
================================================================================
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import sqlite3
import threading

import pytest

from payment_router import DatabaseRepository, PaymentGatewayClient, PaymentRouter

VALID_KEY = "PROD_TEST_SECRET_KEY_123"
VALID_RECIPIENT = "+250780000000"


class FakePaymentGateway(PaymentGatewayClient):
    def __init__(self, should_succeed=True):
        self.should_succeed = should_succeed
        self.calls = []
        self._lock = threading.Lock()

    def process_payment(self, tx_id: str, amount: float, recipient: str) -> bool:
        with self._lock:
            self.calls.append((tx_id, amount, recipient))
        return self.should_succeed


class SQLiteDatabaseRepository(DatabaseRepository):
    """Correct repository bound to the real `transactions` table."""

    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection
        self._lock = threading.Lock()

    def get_transaction(self, tx_id: str) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT tx_id, amount, recipient, status, gateway "
                "FROM transactions WHERE tx_id = ?",
                (tx_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "tx_id": row[0],
            "amount": row[1],
            "recipient": row[2],
            "status": row[3],
            "gateway": row[4],
        }

    def record_transaction(
        self, tx_id: str, amount: float, recipient: str, status: str, gateway: str
    ):
        with self._lock:
            self.conn.execute(
                "INSERT INTO transactions (tx_id, amount, recipient, status, gateway) "
                "VALUES (?, ?, ?, ?, ?)",
                (tx_id, amount, recipient, status, gateway),
            )
            self.conn.commit()


class BuggySQLiteDatabaseRepository(DatabaseRepository):
    """Intentionally broken SQL — wrong table name `tx_history`.

    Unit tests that mock the repo never execute this SQL, so they stay green.
    Integration against a real SQLite schema surfaces OperationalError.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection

    def get_transaction(self, tx_id: str) -> dict:
        row = self.conn.execute(
            "SELECT tx_id, amount, recipient, status, gateway "
            "FROM transactions WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "tx_id": row[0],
            "amount": row[1],
            "recipient": row[2],
            "status": row[3],
            "gateway": row[4],
        }

    def record_transaction(
        self, tx_id: str, amount: float, recipient: str, status: str, gateway: str
    ):
        # MOCK LIE: invalid table name — unit mocks never notice this typo
        self.conn.execute(
            "INSERT INTO tx_history (tx_id, amount, recipient, status, gateway) "
            "VALUES (?, ?, ?, ?, ?)",
            (tx_id, amount, recipient, status, gateway),
        )
        self.conn.commit()


@pytest.fixture(scope="function")
def db_path(tmp_path):
    """Provision a temporary on-disk SQLite DB; delete file after the test."""
    path = tmp_path / "payments.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE transactions (
            tx_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            recipient TEXT NOT NULL,
            status TEXT NOT NULL,
            gateway TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    yield str(path)

    if path.exists():
        path.unlink()


@pytest.fixture(scope="function")
def db_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    yield conn
    conn.close()


@pytest.fixture(scope="function")
def repo(db_connection):
    return SQLiteDatabaseRepository(db_connection)


@pytest.fixture
def primary_gateway():
    return FakePaymentGateway(should_succeed=True)


@pytest.fixture
def backup_gateway():
    return FakePaymentGateway(should_succeed=True)


@pytest.fixture
def router(monkeypatch, repo, primary_gateway, backup_gateway):
    monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", VALID_KEY)
    return PaymentRouter(repo, primary_gateway, backup_gateway)


def test_successful_transaction_persists_primary_success(
    db_connection, router, primary_gateway
):
    result = router.execute_transaction("tx-1", 100.0, VALID_RECIPIENT)

    assert result == "COMPLETED_PRIMARY"
    row = db_connection.execute(
        "SELECT tx_id, amount, recipient, status, gateway "
        "FROM transactions WHERE tx_id = ?",
        ("tx-1",),
    ).fetchone()
    assert row == ("tx-1", 100.0, VALID_RECIPIENT, "SUCCESS", "PRIMARY")
    assert len(primary_gateway.calls) == 1


def test_idempotency_back_to_back_same_tx_id(db_connection, router, primary_gateway):
    """Second call with same tx_id must not insert another row."""
    first = router.execute_transaction("tx-dup", 25.0, VALID_RECIPIENT)
    second = router.execute_transaction("tx-dup", 25.0, VALID_RECIPIENT)

    assert first == "COMPLETED_PRIMARY"
    assert second == "ALREADY_PROCESSED"
    assert len(primary_gateway.calls) == 1

    count = db_connection.execute(
        "SELECT COUNT(*) FROM transactions WHERE tx_id = ?", ("tx-dup",)
    ).fetchone()[0]
    assert count == 1


def test_concurrency_race_enforces_primary_key_constraint(
    monkeypatch, db_path, primary_gateway, backup_gateway
):
    """Two simultaneous calls with the same tx_id exercise UNIQUE(tx_id).

    One thread should complete successfully. The other either observes
    ALREADY_PROCESSED (if the first commit landed before the second check)
    or raises sqlite3.IntegrityError (if both passed the idempotency guard).
    Exactly one SUCCESS row must remain in the table.
    """
    monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", VALID_KEY)

    # Separate connections per thread (SQLite + shared file)
    def make_router():
        conn = sqlite3.connect(db_path, check_same_thread=False)
        repo = SQLiteDatabaseRepository(conn)
        return PaymentRouter(repo, primary_gateway, backup_gateway), conn

    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        router, conn = make_router()
        barrier.wait()
        try:
            result = router.execute_transaction("tx-race", 10.0, VALID_RECIPIENT)
            outcomes.append(("ok", result))
        except Exception as exc:
            outcomes.append(("err", type(exc).__name__))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        for future in as_completed(futures):
            future.result()

    assert any(kind == "ok" and value == "COMPLETED_PRIMARY" for kind, value in outcomes)
    assert all(
        (kind == "ok" and value in {"COMPLETED_PRIMARY", "ALREADY_PROCESSED"})
        or (kind == "err" and value == "IntegrityError")
        for kind, value in outcomes
    )

    verify = sqlite3.connect(db_path)
    try:
        rows = verify.execute(
            "SELECT status, gateway FROM transactions WHERE tx_id = ?",
            ("tx-race",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("SUCCESS", "PRIMARY")
    finally:
        verify.close()

    assert os.path.exists(db_path)


def test_mock_lie_invalid_sql_fails_against_real_database(db_connection, monkeypatch):
    """Integration catches INSERT INTO tx_history; unit mocks would not.

    Written proof (see module docstring): Mock.record_transaction never runs
    SQL, so 100% of unit tests can pass with this typo. Against a real schema
    that only has `transactions`, SQLite raises OperationalError.
    """
    monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", VALID_KEY)
    buggy_repo = BuggySQLiteDatabaseRepository(db_connection)
    gateway = FakePaymentGateway(should_succeed=True)
    router = PaymentRouter(buggy_repo, gateway, FakePaymentGateway(should_succeed=False))

    with pytest.raises(sqlite3.OperationalError, match="no such table: tx_history"):
        router.execute_transaction("tx-lie", 40.0, VALID_RECIPIENT)
