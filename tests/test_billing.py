"""Tests for the billing engine: plan constants, quota math, usage costing,
invoices, API keys, and Midtrans webhook signature verification.

All tests use a temporary SQLite DB. No network calls.
"""

import hashlib
import sqlite3

import pytest

import billing
from billing import BillingEngine, PLANS


@pytest.fixture()
def engine(tmp_path):
    return BillingEngine(db_path=str(tmp_path / "billing-test.db"))


def _set_used_tokens(engine, api_key, used):
    """Force the account's cycle usage to a given value (test helper)."""
    prefix = billing._api_key_prefix(api_key)
    conn = sqlite3.connect(engine.db_path)
    conn.execute(
        "UPDATE accounts SET tokens_used_this_cycle=? WHERE api_key_prefix=?",
        (used, prefix),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Plan constants (audit-critical values)
# ---------------------------------------------------------------------------

class TestPlanConstants:
    def test_free_tier_quota_is_100k(self):
        assert PLANS["free"]["monthly_quota_tokens"] == 100_000
        assert PLANS["free"]["overage_allowed"] is False
        assert PLANS["free"]["monthly_price_idr"] == 0

    def test_overage_markups(self):
        # Starter/Pro pay +20% on overage, Business +10%
        assert PLANS["starter"]["overage_markup"] == pytest.approx(1.2)
        assert PLANS["pro"]["overage_markup"] == pytest.approx(1.2)
        assert PLANS["business"]["overage_markup"] == pytest.approx(1.1)
        assert PLANS["free"]["overage_markup"] == pytest.approx(1.0)

    def test_paid_plan_quotas(self):
        assert PLANS["starter"]["monthly_quota_tokens"] == 5_000_000
        assert PLANS["pro"]["monthly_quota_tokens"] == 20_000_000
        assert PLANS["business"]["monthly_quota_tokens"] == 100_000_000


# ---------------------------------------------------------------------------
# Accounts & API keys
# ---------------------------------------------------------------------------

class TestAccountsAndKeys:
    def test_create_free_account_gets_100k_quota(self, engine):
        acct = engine.create_account("opsk-create-free-001", "free")
        assert acct["plan"] == "free"
        assert acct["monthly_quota_tokens"] == 100_000
        assert acct["status"] == "active"

    def test_create_account_unknown_plan_rejected(self, engine):
        assert "error" in engine.create_account("opsk-x-1", "platinum")

    def test_issue_validate_revoke_key_roundtrip(self, engine):
        issued = engine.issue_api_key(email="dev@example.com", plan="free")
        assert issued["api_key"].startswith("opsk-")
        assert issued["monthly_quota_tokens"] == 100_000

        valid = engine.validate_api_key(issued["api_key"])
        assert valid is not None
        assert valid["account_id"] == issued["account_id"]
        assert valid["email"] == "dev@example.com"

        assert engine.validate_api_key("opsk-does-not-exist") is None
        assert engine.validate_api_key("") is None
        assert engine.validate_api_key(None) is None

        assert engine.revoke_api_key(issued["api_key"]) is True
        assert engine.validate_api_key(issued["api_key"]) is None


# ---------------------------------------------------------------------------
# Quota enforcement math
# ---------------------------------------------------------------------------

class TestQuotaMath:
    def test_free_tier_within_quota_allowed(self, engine):
        key = "opsk-quota-free-001"
        engine.create_account(key, "free")
        allowed, info = engine.check_quota(key, "opsora-fast", estimated_tokens=50_000)
        assert allowed is True
        assert info["remaining_tokens"] == 100_000
        assert info["plan"] == "free"

    def test_free_tier_exact_quota_blocked(self, engine):
        key = "opsk-quota-free-002"
        engine.create_account(key, "free")
        # remaining (100_000) <= estimated (100_000) -> hard block
        allowed, info = engine.check_quota(key, "opsora-fast", estimated_tokens=100_000)
        assert allowed is False
        assert info["reason"] == "Quota exceeded"
        assert "upgrade_hint" in info

    def test_free_tier_just_under_quota_allowed(self, engine):
        key = "opsk-quota-free-003"
        engine.create_account(key, "free")
        allowed, _ = engine.check_quota(key, "opsora-fast", estimated_tokens=99_999)
        assert allowed is True

    def test_paid_plan_overage_capped_at_20_percent(self, engine):
        key = "opsk-quota-starter-001"
        engine.create_account(key, "starter")  # 5M quota
        overage_limit = int(5_000_000 * 0.2)  # 1M

        # Up to quota + 20% is allowed
        allowed, _ = engine.check_quota(
            key, "opsora-fast", estimated_tokens=5_000_000 + overage_limit - 1)
        assert allowed is True

        # At/over quota + 20% it is blocked
        allowed, info = engine.check_quota(
            key, "opsora-fast", estimated_tokens=5_000_000 + overage_limit)
        assert allowed is False
        assert info["reason"] == "Overage limit exceeded"

    def test_unknown_key_auto_creates_free_account(self, engine):
        allowed, info = engine.check_quota("opsk-brand-new-key", "opsora-fast",
                                           estimated_tokens=10)
        assert allowed is True
        assert info["plan"] == "free"


# ---------------------------------------------------------------------------
# Usage recording & cost math
# ---------------------------------------------------------------------------

class TestUsageCosting:
    def test_within_quota_usage_no_markup(self, engine):
        key = "opsk-usage-starter-001"
        engine.create_account(key, "starter")
        # 1k in + 1k out on opsora-fast: (0.30 + 0.60) per 1M tokens
        engine.record_usage(key, "opsora-fast", 1_000, 1_000)

        base_usd = (1_000 / 1_000_000) * 0.30 + (1_000 / 1_000_000) * 0.60
        expected_idr = int(base_usd * billing.IDR_USD_RATE)

        conn = sqlite3.connect(engine.db_path)
        row = conn.execute(
            "SELECT type, amount_idr, tokens FROM transactions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == "usage"
        assert row[1] == expected_idr
        assert row[2] == 2_000

    def test_overage_on_starter_applies_20_percent_markup(self, engine):
        key = "opsk-usage-starter-002"
        engine.create_account(key, "starter")  # 5M quota
        _set_used_tokens(engine, key, 4_999_900)  # only 100 tokens remain

        engine.record_usage(key, "opsora-fast", 1_000_000, 0)

        base_usd = (1_000_000 / 1_000_000) * 0.30
        expected_idr = int(base_usd * PLANS["starter"]["overage_markup"]
                           * billing.IDR_USD_RATE)

        conn = sqlite3.connect(engine.db_path)
        row = conn.execute(
            "SELECT type, amount_idr, tokens FROM transactions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        used = conn.execute(
            "SELECT tokens_used_this_cycle FROM accounts WHERE api_key_prefix=?",
            (billing._api_key_prefix(key),),
        ).fetchone()[0]
        conn.close()
        assert row[0] == "overage"
        assert row[1] == expected_idr
        assert used == 4_999_900 + 1_000_000

    def test_overage_on_free_tier_keeps_1x_markup(self, engine):
        key = "opsk-usage-free-001"
        engine.create_account(key, "free")
        engine.record_usage(key, "opsora-fast", 1_000_000, 0)

        expected_idr = int(0.30 * PLANS["free"]["overage_markup"]
                           * billing.IDR_USD_RATE)
        conn = sqlite3.connect(engine.db_path)
        row = conn.execute(
            "SELECT type, amount_idr FROM transactions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == "overage"  # over quota, but free plan markup is 1.0
        assert row[1] == expected_idr


# ---------------------------------------------------------------------------
# Invoices: total = base + overage
# ---------------------------------------------------------------------------

class TestInvoices:
    def test_invoice_total_is_base_plus_overage(self, engine):
        key = "opsk-invoice-starter-001"
        acct = engine.create_account(key, "starter")
        _set_used_tokens(engine, key, 4_999_900)
        engine.record_usage(key, "opsora-fast", 1_000_000, 0)  # overage txn

        overage_idr = int(0.30 * PLANS["starter"]["overage_markup"]
                          * billing.IDR_USD_RATE)

        invoice = engine.generate_invoice(
            acct["id"], billing._cycle_start(), billing._cycle_end()
        )
        assert invoice["base_amount_idr"] == PLANS["starter"]["monthly_price_idr"]
        assert invoice["overage_amount_idr"] == overage_idr
        assert invoice["total_idr"] == (invoice["base_amount_idr"]
                                        + invoice["overage_amount_idr"])
        assert invoice["status"] == "pending"

    def test_invoice_without_overage_is_base_only(self, engine):
        key = "opsk-invoice-pro-001"
        acct = engine.create_account(key, "pro")
        invoice = engine.generate_invoice(
            acct["id"], billing._cycle_start(), billing._cycle_end()
        )
        assert invoice["base_amount_idr"] == PLANS["pro"]["monthly_price_idr"]
        assert invoice["overage_amount_idr"] == 0
        assert invoice["total_idr"] == invoice["base_amount_idr"]

    def test_subscription_invoice_for_free_plan_rejected(self, engine):
        key = "opsk-invoice-free-001"
        engine.create_account(key, "free")
        assert "error" in engine.create_subscription_invoice(key, "free")
        assert "error" in engine.create_subscription_invoice(key, "enterprise")
        assert "error" in engine.create_subscription_invoice(key, "nope")


# ---------------------------------------------------------------------------
# Midtrans webhook HMAC (SHA-512 signature)
# ---------------------------------------------------------------------------

SERVER_KEY = "SB-Mid-server-test-key-123"


def _midtrans_sig(order_id, status_code, gross_amount, server_key):
    raw = f"{order_id}{status_code}{gross_amount}{server_key}"
    return hashlib.sha512(raw.encode()).hexdigest()


def _create_invoice_with_order(engine, key, order_id):
    engine.create_account(key, "free")
    invoice = engine.create_subscription_invoice(key, "starter")
    assert "error" not in invoice
    conn = sqlite3.connect(engine.db_path)
    conn.execute("UPDATE invoices SET midtrans_order_id=? WHERE id=?",
                 (order_id, invoice["id"]))
    conn.commit()
    conn.close()
    return invoice


class TestMidtransWebhook:
    def test_valid_signature_marks_paid_and_activates_plan(self, engine, monkeypatch):
        monkeypatch.setattr(billing, "MIDTRANS_SERVER_KEY", SERVER_KEY)
        key = "opsk-midtrans-valid-001"
        order_id = "OPSORA-TEST-ORDER-1"
        _create_invoice_with_order(engine, key, order_id)

        notification = {
            "order_id": order_id,
            "status_code": "200",
            "gross_amount": "490000.00",
            "signature_key": _midtrans_sig(order_id, "200", "490000.00", SERVER_KEY),
            "transaction_status": "settlement",
        }
        result = engine.handle_midtrans_notification(notification)
        assert result["status"] == "paid"
        assert result["activated_plan"] == "starter"

        acct = engine._get_account(key)
        assert acct["plan"] == "starter"
        assert acct["monthly_quota_tokens"] == PLANS["starter"]["monthly_quota_tokens"]
        assert acct["tokens_used_this_cycle"] == 0  # fresh cycle on activation

    def test_tampered_amount_rejected(self, engine, monkeypatch):
        monkeypatch.setattr(billing, "MIDTRANS_SERVER_KEY", SERVER_KEY)
        key = "opsk-midtrans-tampered-001"
        order_id = "OPSORA-TEST-ORDER-2"
        invoice = _create_invoice_with_order(engine, key, order_id)

        # Signature computed for 490000.00, but attacker claims 1.00
        notification = {
            "order_id": order_id,
            "status_code": "200",
            "gross_amount": "1.00",
            "signature_key": _midtrans_sig(order_id, "200", "490000.00", SERVER_KEY),
            "transaction_status": "settlement",
        }
        result = engine.handle_midtrans_notification(notification)
        assert result == {"error": "Invalid signature"}

        # Invoice must remain unpaid
        invoices = engine.get_invoices(invoice["account_id"])
        assert invoices[0]["status"] == "pending"
        assert engine._get_account(key)["plan"] == "free"

    def test_tampered_order_id_rejected(self, engine, monkeypatch):
        monkeypatch.setattr(billing, "MIDTRANS_SERVER_KEY", SERVER_KEY)
        key = "opsk-midtrans-tampered-002"
        order_id = "OPSORA-TEST-ORDER-3"
        _create_invoice_with_order(engine, key, order_id)

        notification = {
            "order_id": "OPSORA-TEST-ORDER-3",
            "status_code": "200",
            "gross_amount": "490000.00",
            # signature for a DIFFERENT order id
            "signature_key": _midtrans_sig("OPSORA-OTHER-ORDER", "200",
                                           "490000.00", SERVER_KEY),
            "transaction_status": "settlement",
        }
        result = engine.handle_midtrans_notification(notification)
        assert result == {"error": "Invalid signature"}

    def test_unconfigured_midtrans_rejected(self, engine, monkeypatch):
        monkeypatch.setattr(billing, "MIDTRANS_SERVER_KEY", "")
        result = engine.handle_midtrans_notification({"order_id": "X"})
        assert result == {"error": "Midtrans not configured"}

    def test_valid_signature_unknown_order_rejected(self, engine, monkeypatch):
        monkeypatch.setattr(billing, "MIDTRANS_SERVER_KEY", SERVER_KEY)
        order_id = "OPSORA-NEVER-CREATED"
        notification = {
            "order_id": order_id,
            "status_code": "200",
            "gross_amount": "490000.00",
            "signature_key": _midtrans_sig(order_id, "200", "490000.00", SERVER_KEY),
            "transaction_status": "settlement",
        }
        result = engine.handle_midtrans_notification(notification)
        assert result == {"error": "Invoice not found"}
