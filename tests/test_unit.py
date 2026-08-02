from unittest.mock import Mock, patch

import pytest

from payment_router import PaymentRouter

VALID_KEY = "PROD_TEST_SECRET_KEY_123"
VALID_RECIPIENT = "+250780000000"


@pytest.fixture
def mock_repo():
    return Mock()


@pytest.fixture
def mock_primary():
    return Mock()


@pytest.fixture
def mock_backup():
    return Mock()


@pytest.fixture
def router(monkeypatch, mock_repo, mock_primary, mock_backup):
    monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", VALID_KEY)
    return PaymentRouter(mock_repo, mock_primary, mock_backup)


class TestSecurityAndEnvironment:
    def test_missing_api_key_raises_permission_error(
        self, monkeypatch, mock_repo, mock_primary, mock_backup
    ):
        monkeypatch.delenv("PAYMENT_GATEWAY_API_KEY", raising=False)
        router = PaymentRouter(mock_repo, mock_primary, mock_backup)

        with pytest.raises(PermissionError, match="Unauthorized"):
            router.execute_transaction("tx-1", 100.0, VALID_RECIPIENT)

        mock_repo.get_transaction.assert_not_called()
        mock_primary.process_payment.assert_not_called()
        mock_backup.process_payment.assert_not_called()

    def test_debug_mode_key_raises_permission_error(
        self, monkeypatch, mock_repo, mock_primary, mock_backup
    ):
        monkeypatch.setenv("PAYMENT_GATEWAY_API_KEY", "DEBUG_MODE_KEY")
        router = PaymentRouter(mock_repo, mock_primary, mock_backup)

        with pytest.raises(PermissionError, match="insecure default"):
            router.execute_transaction("tx-1", 100.0, VALID_RECIPIENT)

        mock_primary.process_payment.assert_not_called()
        mock_backup.process_payment.assert_not_called()


class TestValidation:
    @pytest.mark.parametrize("amount", [0, -1, -100.5])
    def test_non_positive_amount_raises_value_error(self, router, mock_repo, amount):
        with pytest.raises(ValueError, match="Invalid transaction amount"):
            router.execute_transaction("tx-1", amount, VALID_RECIPIENT)

        mock_repo.get_transaction.assert_not_called()

    @pytest.mark.parametrize(
        "recipient",
        ["0780000000", "+00012", "invalid", "", "+", "250780000000"],
    )
    def test_invalid_phone_raises_value_error(self, router, mock_repo, recipient):
        with pytest.raises(ValueError, match="Invalid E.164 phone number format"):
            router.execute_transaction("tx-1", 100.0, recipient)

        mock_repo.get_transaction.assert_not_called()


class TestIdempotency:
    def test_already_processed_skips_both_gateways(
        self, router, mock_repo, mock_primary, mock_backup
    ):
        mock_repo.get_transaction.return_value = {
            "tx_id": "tx-1",
            "status": "SUCCESS",
            "gateway": "PRIMARY",
        }

        result = router.execute_transaction("tx-1", 100.0, VALID_RECIPIENT)

        assert result == "ALREADY_PROCESSED"
        mock_primary.process_payment.assert_not_called()
        mock_backup.process_payment.assert_not_called()
        mock_repo.record_transaction.assert_not_called()


class TestFlakyGatewayRetry:
    def test_primary_fails_once_then_succeeds_with_sleep(
        self, router, mock_repo, mock_primary, mock_backup
    ):
        mock_repo.get_transaction.return_value = None
        mock_primary.process_payment.side_effect = [
            TimeoutError("network timeout"),
            True,
        ]

        with patch("payment_router.time.sleep") as mock_sleep:
            result = router.execute_transaction("tx-1", 50.0, VALID_RECIPIENT)

        assert result == "COMPLETED_PRIMARY"
        assert mock_primary.process_payment.call_count == 2
        mock_sleep.assert_called_once_with(0.1)
        mock_backup.process_payment.assert_not_called()
        mock_repo.record_transaction.assert_called_once_with(
            "tx-1", 50.0, VALID_RECIPIENT, "SUCCESS", "PRIMARY"
        )


class TestFallbackGateway:
    def test_primary_exhausted_backup_succeeds(
        self, router, mock_repo, mock_primary, mock_backup
    ):
        mock_repo.get_transaction.return_value = None
        mock_primary.process_payment.return_value = False
        mock_backup.process_payment.return_value = True

        with patch("payment_router.time.sleep"):
            result = router.execute_transaction("tx-1", 75.0, VALID_RECIPIENT)

        assert result == "COMPLETED_BACKUP"
        assert mock_primary.process_payment.call_count == 2
        mock_backup.process_payment.assert_called_once_with(
            "tx-1", 75.0, VALID_RECIPIENT
        )
        mock_repo.record_transaction.assert_called_once_with(
            "tx-1", 75.0, VALID_RECIPIENT, "SUCCESS", "BACKUP"
        )


class TestCompleteSystemCircuitBreak:
    def test_both_gateways_http_500_logs_failed_and_raises(
        self, router, mock_repo, mock_primary, mock_backup
    ):
        mock_repo.get_transaction.return_value = None
        mock_primary.process_payment.side_effect = Exception("HTTP 500 Internal Server Error")
        mock_backup.process_payment.side_effect = Exception("HTTP 500 Internal Server Error")

        with patch("payment_router.time.sleep") as mock_sleep:
            with pytest.raises(
                RuntimeError, match="Payment routing failed across all gateways"
            ):
                router.execute_transaction("tx-1", 100.0, VALID_RECIPIENT)

        assert mock_primary.process_payment.call_count == 2
        assert mock_sleep.call_count == 2
        mock_backup.process_payment.assert_called_once()
        mock_repo.record_transaction.assert_called_once_with(
            "tx-1", 100.0, VALID_RECIPIENT, "FAILED", "NONE"
        )
