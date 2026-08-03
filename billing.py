#!/usr/bin/env python3
"""Opsora Agent API — Billing Engine.

Token-based billing with subscription plans, quota enforcement,
invoice generation, and Midtrans payment integration.

Zero external dependencies — Python stdlib only."""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from base64 import b64encode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger("opsora")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IDR_USD_RATE = float(os.getenv("IDR_USD_RATE", "16100"))
MIDTRANS_SERVER_KEY = os.getenv("MIDTRANS_SERVER_KEY", "")
MIDTRANS_SANDBOX = os.getenv("MIDTRANS_SANDBOX", "true").lower() == "true"
MIDTRANS_BASE = (
    "https://app.sandbox.midtrans.com/snap/v1"
    if MIDTRANS_SANDBOX
    else "https://app.midtrans.com/snap/v1"
)

# ---------------------------------------------------------------------------
# Pricing Tables
# ---------------------------------------------------------------------------

MODEL_PRICING = {
    "opsora-fast":   {"input": 0.30, "output": 0.60, "label": "DeepSeek V4 Flash"},
    "opsora-brain":  {"input": 0.80, "output": 2.00, "label": "Llama 3.1 70B"},
    "opsora-code":   {"input": 0.80, "output": 2.00, "label": "Nemotron Super 49B"},
    "opsora-vision": {"input": 1.00, "output": 2.50, "label": "Llama 3.2 90B Vision"},
    "opsora-reason": {"input": 1.50, "output": 4.00, "label": "DeepSeek V4 Pro"},
    "opsora-max":    {"input": 3.00, "output": 8.00, "label": "Nemotron Ultra 550B"},
}

PLANS = {
    "free": {
        "name": "Free",
        "monthly_price_idr": 0,
        "monthly_price_usd": 0,
        "monthly_quota_tokens": 100_000,
        "rate_limit_rpm": 60,
        "overage_allowed": False,
        "overage_markup": 1.0,
        "features": ["All models", "Community support"],
    },
    "starter": {
        "name": "Starter",
        "monthly_price_idr": 490_000,
        "monthly_price_usd": 30,
        "monthly_quota_tokens": 5_000_000,
        "rate_limit_rpm": 120,
        "overage_allowed": True,
        "overage_markup": 1.2,
        "features": ["All models", "Email support", "Webhooks"],
    },
    "pro": {
        "name": "Pro",
        "monthly_price_idr": 990_000,
        "monthly_price_usd": 60,
        "monthly_quota_tokens": 20_000_000,
        "rate_limit_rpm": 300,
        "overage_allowed": True,
        "overage_markup": 1.2,
        "features": ["All models", "Priority support", "Webhooks", "Usage analytics"],
    },
    "business": {
        "name": "Business",
        "monthly_price_idr": 2_490_000,
        "monthly_price_usd": 155,
        "monthly_quota_tokens": 100_000_000,
        "rate_limit_rpm": 600,
        "overage_allowed": True,
        "overage_markup": 1.1,
        "features": ["All models", "Dedicated support", "SLA 99.9%", "Custom webhooks", "Team management"],
    },
    "enterprise": {
        "name": "Enterprise",
        "monthly_price_idr": None,
        "monthly_price_usd": None,
        "monthly_quota_tokens": None,
        "rate_limit_rpm": None,
        "overage_allowed": True,
        "overage_markup": 1.0,
        "features": ["Everything in Business", "Custom models", "Dedicated infrastructure", "24/7 support"],
    },
}


def _api_key_prefix(api_key):
    """Extract a safe prefix from an API key for identification."""
    if not api_key:
        return "unknown"
    return api_key[:12] + "..." if len(api_key) > 12 else api_key


def _now():
    return time.time()


def _cycle_start():
    """Start of current billing cycle (1st of current month, midnight UTC)."""
    import calendar
    now = time.gmtime()
    return calendar.timegm((now.tm_year, now.tm_mon, 1, 0, 0, 0))


def _cycle_end():
    """End of current billing cycle (1st of next month, midnight UTC)."""
    import calendar
    now = time.gmtime()
    year = now.tm_year
    month = now.tm_mon + 1
    if month > 12:
        month = 1
        year += 1
    return calendar.timegm((year, month, 1, 0, 0, 0))


# ---------------------------------------------------------------------------
# Billing Engine
# ---------------------------------------------------------------------------


class BillingEngine:
    """Token-based billing with subscription plans and quota enforcement."""

    def __init__(self, db_path="billing.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Create billing tables if they don't exist."""
        with self._lock:
            conn = self._conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    api_key_prefix TEXT NOT NULL UNIQUE,
                    plan TEXT NOT NULL DEFAULT 'free',
                    created_at REAL NOT NULL,
                    billing_cycle_start REAL NOT NULL,
                    monthly_quota_tokens INTEGER NOT NULL DEFAULT 100000,
                    tokens_used_this_cycle INTEGER NOT NULL DEFAULT 0,
                    balance_idr INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    type TEXT NOT NULL,
                    amount_idr INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    model TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    base_amount_idr INTEGER NOT NULL DEFAULT 0,
                    overage_amount_idr INTEGER NOT NULL DEFAULT 0,
                    total_idr INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    paid_at REAL,
                    midtrans_order_id TEXT,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                );

                CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
                CREATE INDEX IF NOT EXISTS idx_txn_ts ON transactions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_inv_account ON invoices(account_id);
                CREATE INDEX IF NOT EXISTS idx_accounts_prefix ON accounts(api_key_prefix);

                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY,
                    key_prefix TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                );
            """)
            # Migration: invoices created before plan linkage lack the column.
            try:
                conn.execute("ALTER TABLE invoices ADD COLUMN plan TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()
            conn.close()
        logger.info("Billing DB initialized: %s", self.db_path)

    # ------------------------------------------------------------------
    # Account Management
    # ------------------------------------------------------------------

    def create_account(self, api_key, plan="free"):
        """Create a billing account for an API key.

        Args:
            api_key: The full API key string
            plan: Plan name (free, starter, pro, business, enterprise)

        Returns:
            dict with account details
        """
        if plan not in PLANS:
            return {"error": f"Unknown plan: {plan}"}

        plan_config = PLANS[plan]
        prefix = _api_key_prefix(api_key)
        account_id = str(uuid.uuid4())
        now = _now()
        cycle_start = _cycle_start()
        quota = plan_config["monthly_quota_tokens"] or 0

        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO accounts (id, api_key_prefix, plan, created_at, "
                    "billing_cycle_start, monthly_quota_tokens, tokens_used_this_cycle, "
                    "balance_idr, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (account_id, prefix, plan, now, cycle_start, quota, 0, 0, "active"),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Account already exists for this prefix
                row = conn.execute(
                    "SELECT id, plan, status FROM accounts WHERE api_key_prefix=?",
                    (prefix,),
                ).fetchone()
                conn.close()
                if row:
                    return {
                        "id": row[0],
                        "api_key_prefix": prefix,
                        "plan": row[1],
                        "status": row[2],
                        "message": "Account already exists",
                    }
                return {"error": "Failed to create account"}
            conn.close()

        logger.info("Created billing account: %s plan=%s", prefix, plan)
        return {
            "id": account_id,
            "api_key_prefix": prefix,
            "plan": plan,
            "plan_name": plan_config["name"],
            "monthly_quota_tokens": quota,
            "rate_limit_rpm": plan_config["rate_limit_rpm"],
            "status": "active",
        }

    # ------------------------------------------------------------------
    # API Key Management (self-serve issuance, hashed storage)
    # ------------------------------------------------------------------

    def issue_api_key(self, email="", plan="free"):
        """Create a new account and issue an API key for it.

        The full key is returned exactly once and stored only as a SHA-256
        hash — the plaintext key is never persisted.

        Args:
            email: Customer email (for account recovery / invoices)
            plan: Initial plan (default free)

        Returns:
            dict with api_key (shown once), account_id, plan
        """
        raw = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        api_key = f"opsk-{raw}"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        account = self.create_account(api_key, plan)
        if "error" in account:
            return account

        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, account_id, email, "
                "created_at, status) VALUES (?,?,?,?,?,?)",
                (key_hash, _api_key_prefix(api_key), account["id"], email,
                 _now(), "active"),
            )
            conn.commit()
            conn.close()

        logger.info("Issued API key %s (account=%s, email=%s)",
                    _api_key_prefix(api_key), account["id"][:8], email or "-")
        return {
            "api_key": api_key,
            "key_prefix": _api_key_prefix(api_key),
            "account_id": account["id"],
            "plan": account["plan"],
            "monthly_quota_tokens": account["monthly_quota_tokens"],
        }

    def validate_api_key(self, api_key):
        """Validate an issued API key by hash lookup.

        Returns:
            dict with account_id, plan, email, status — or None if the key
            is unknown or revoked.
        """
        if not api_key or not api_key.startswith("opsk-"):
            return None
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT account_id, email, status FROM api_keys WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
            conn.close()
        if not row or row[2] != "active":
            return None
        return {"account_id": row[0], "email": row[1], "status": row[2]}

    def revoke_api_key(self, api_key):
        """Revoke an issued API key."""
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "UPDATE api_keys SET status='revoked' WHERE key_hash=?",
                (key_hash,),
            )
            conn.commit()
            revoked = cur.rowcount > 0
            conn.close()
        return revoked

    def _get_account(self, api_key):
        """Get account by API key prefix. Returns row dict or None."""
        prefix = _api_key_prefix(api_key)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT id, api_key_prefix, plan, created_at, billing_cycle_start, "
                "monthly_quota_tokens, tokens_used_this_cycle, balance_idr, status "
                "FROM accounts WHERE api_key_prefix=?",
                (prefix,),
            ).fetchone()
            conn.close()
        if not row:
            return None
        return {
            "id": row[0],
            "api_key_prefix": row[1],
            "plan": row[2],
            "created_at": row[3],
            "billing_cycle_start": row[4],
            "monthly_quota_tokens": row[5],
            "tokens_used_this_cycle": row[6],
            "balance_idr": row[7],
            "status": row[8],
        }

    def upgrade_plan(self, api_key, new_plan):
        """Upgrade or downgrade plan. Prorate remaining quota.

        Returns:
            dict with updated account details
        """
        if new_plan not in PLANS:
            return {"error": f"Unknown plan: {new_plan}"}

        account = self._get_account(api_key)
        if not account:
            return {"error": "Account not found"}

        old_plan = account["plan"]
        new_config = PLANS[new_plan]
        new_quota = new_config["monthly_quota_tokens"] or 0

        # Prorate: calculate remaining days and adjust quota
        cycle_start = account["billing_cycle_start"]
        cycle_end_ts = _cycle_end()
        cycle_duration = cycle_end_ts - cycle_start
        remaining = max(0, cycle_end_ts - _now())
        remaining_fraction = remaining / cycle_duration if cycle_duration > 0 else 0

        old_quota = account["monthly_quota_tokens"]
        prorated_new = int(new_quota * remaining_fraction)
        # Keep used tokens, adjust remaining
        additional_quota = max(0, prorated_new - (old_quota - account["tokens_used_this_cycle"]))

        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE accounts SET plan=?, monthly_quota_tokens=monthly_quota_tokens+? "
                "WHERE id=?",
                (new_plan, additional_quota, account["id"]),
            )
            conn.execute(
                "INSERT INTO transactions (id, account_id, timestamp, type, amount_idr, "
                "tokens, model, description) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), account["id"], _now(), "plan_change", 0,
                 additional_quota, "", f"Plan change: {old_plan} → {new_plan}"),
            )
            conn.commit()
            conn.close()

        logger.info("Plan upgraded: %s %s → %s", account["api_key_prefix"], old_plan, new_plan)
        return {
            "account_id": account["id"],
            "old_plan": old_plan,
            "new_plan": new_plan,
            "new_plan_name": new_config["name"],
            "additional_quota_tokens": additional_quota,
            "rate_limit_rpm": new_config["rate_limit_rpm"],
        }

    # ------------------------------------------------------------------
    # Quota Checking & Usage Recording
    # ------------------------------------------------------------------

    def check_quota(self, api_key, model, estimated_tokens=0):
        """Check if a request is within quota.

        Args:
            api_key: API key string
            model: Model alias (e.g. 'opsora-fast')
            estimated_tokens: Estimated token count for this request

        Returns:
            (allowed: bool, info: dict)
        """
        account = self._get_account(api_key)
        if not account:
            # No account = auto-create free account
            self.create_account(api_key, "free")
            account = self._get_account(api_key)

        if account["status"] != "active":
            return False, {"reason": "Account suspended", "plan": account["plan"]}

        # Check if cycle needs reset
        if _now() >= _cycle_end():
            self.reset_cycle(api_key)
            account = self._get_account(api_key)

        plan_config = PLANS.get(account["plan"], PLANS["free"])
        remaining = account["monthly_quota_tokens"] - account["tokens_used_this_cycle"]
        percentage_used = (
            (account["tokens_used_this_cycle"] / account["monthly_quota_tokens"] * 100)
            if account["monthly_quota_tokens"] > 0
            else 100
        )

        # Free plan: hard block
        if not plan_config["overage_allowed"] and remaining <= estimated_tokens:
            return False, {
                "reason": "Quota exceeded",
                "plan": account["plan"],
                "remaining_tokens": max(0, remaining),
                "quota_tokens": account["monthly_quota_tokens"],
                "upgrade_hint": "Upgrade to Starter for 5M tokens/month",
            }

        # Paid plans: allow overage up to a limit
        if plan_config["overage_allowed"]:
            overage_limit = int(account["monthly_quota_tokens"] * 0.2)
            if remaining + overage_limit <= estimated_tokens:
                return False, {
                    "reason": "Overage limit exceeded",
                    "plan": account["plan"],
                    "remaining_tokens": remaining,
                    "overage_limit": overage_limit,
                }

        return True, {
            "plan": account["plan"],
            "remaining_tokens": max(0, remaining),
            "quota_tokens": account["monthly_quota_tokens"],
            "percentage_used": round(percentage_used, 1),
        }

    def record_usage(self, api_key, model, input_tokens, output_tokens):
        """Record token usage and calculate cost.

        Args:
            api_key: API key string
            model: Model alias
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
        """
        account = self._get_account(api_key)
        if not account:
            self.create_account(api_key, "free")
            account = self._get_account(api_key)

        total_tokens = input_tokens + output_tokens
        pricing = MODEL_PRICING.get(model, MODEL_PRICING.get("opsora-fast"))

        # Calculate cost in USD
        input_cost_usd = (input_tokens / 1_000_000) * pricing["input"]
        output_cost_usd = (output_tokens / 1_000_000) * pricing["output"]
        base_cost_usd = input_cost_usd + output_cost_usd

        # Check overage
        remaining = account["monthly_quota_tokens"] - account["tokens_used_this_cycle"]
        is_overage = remaining < total_tokens
        overage_tokens = max(0, total_tokens - max(0, remaining))

        plan_config = PLANS.get(account["plan"], PLANS["free"])
        markup = plan_config.get("overage_markup", 1.0) if is_overage else 1.0
        total_cost_usd = base_cost_usd * markup if is_overage else base_cost_usd
        total_cost_idr = int(total_cost_usd * IDR_USD_RATE)

        with self._lock:
            conn = self._conn()
            # Update tokens used
            conn.execute(
                "UPDATE accounts SET tokens_used_this_cycle = tokens_used_this_cycle + ? "
                "WHERE id=?",
                (total_tokens, account["id"]),
            )
            # Record transaction
            txn_type = "overage" if is_overage else "usage"
            description = (
                f"{model}: {input_tokens}in + {output_tokens}out tokens"
                + (f" ({overage_tokens} overage)" if is_overage else "")
            )
            conn.execute(
                "INSERT INTO transactions (id, account_id, timestamp, type, amount_idr, "
                "tokens, model, description) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), account["id"], _now(), txn_type,
                 total_cost_idr, total_tokens, model, description),
            )
            conn.commit()
            conn.close()

        logger.info(
            "Billing: %s %s +%d tokens (cost: Rp %s)%s",
            _api_key_prefix(api_key), model, total_tokens,
            f"{total_cost_idr:,}", " [OVERAGE]" if is_overage else "",
        )

    # ------------------------------------------------------------------
    # Usage Summary
    # ------------------------------------------------------------------

    def get_usage_summary(self, api_key):
        """Get billing summary for current cycle.

        Returns:
            dict with plan info, usage, quota, costs
        """
        account = self._get_account(api_key)
        if not account:
            return {"error": "Account not found"}

        plan_config = PLANS.get(account["plan"], PLANS["free"])
        remaining = account["monthly_quota_tokens"] - account["tokens_used_this_cycle"]
        percentage = (
            (account["tokens_used_this_cycle"] / account["monthly_quota_tokens"] * 100)
            if account["monthly_quota_tokens"] > 0
            else 0
        )

        cycle_end = _cycle_end()
        days_remaining = max(0, (cycle_end - _now()) / 86400)

        # Get cost breakdown from transactions
        with self._lock:
            conn = self._conn()
            cycle_start = account["billing_cycle_start"]
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_idr), 0), COUNT(*) "
                "FROM transactions WHERE account_id=? AND timestamp>=?",
                (account["id"], cycle_start),
            ).fetchone()
            total_cost_idr = row[0]
            total_transactions = row[1]

            # Per-model breakdown
            model_rows = conn.execute(
                "SELECT model, SUM(tokens), SUM(amount_idr), COUNT(*) "
                "FROM transactions WHERE account_id=? AND timestamp>=? "
                "GROUP BY model ORDER BY SUM(tokens) DESC",
                (account["id"], cycle_start),
            ).fetchall()
            conn.close()

        per_model = []
        for r in model_rows:
            per_model.append({
                "model": r[0],
                "tokens": r[1],
                "cost_idr": r[2],
                "requests": r[3],
            })

        return {
            "plan": account["plan"],
            "plan_name": plan_config["name"],
            "monthly_quota_tokens": account["monthly_quota_tokens"],
            "tokens_used_this_cycle": account["tokens_used_this_cycle"],
            "remaining_tokens": max(0, remaining),
            "percentage_used": round(percentage, 1),
            "total_cost_idr": total_cost_idr,
            "total_transactions": total_transactions,
            "days_remaining_in_cycle": round(days_remaining, 1),
            "cycle_end": cycle_end,
            "per_model": per_model,
            "alerts": self.check_alerts(api_key),
        }

    def get_pricing_table(self):
        """Return the full pricing table for API response."""
        result = {"plans": {}, "model_pricing": {}}

        for plan_id, config in PLANS.items():
            result["plans"][plan_id] = {
                "name": config["name"],
                "monthly_price_idr": config["monthly_price_idr"],
                "monthly_price_usd": config["monthly_price_usd"],
                "monthly_quota_tokens": config["monthly_quota_tokens"],
                "rate_limit_rpm": config["rate_limit_rpm"],
                "features": config["features"],
            }

        for model_id, pricing in MODEL_PRICING.items():
            result["model_pricing"][model_id] = {
                "label": pricing["label"],
                "input_per_1m_usd": pricing["input"],
                "output_per_1m_usd": pricing["output"],
                "input_per_1m_idr": int(pricing["input"] * IDR_USD_RATE),
                "output_per_1m_idr": int(pricing["output"] * IDR_USD_RATE),
            }

        result["exchange_rate"] = {"idr_usd": IDR_USD_RATE}
        return result

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    def generate_invoice(self, account_id, period_start, period_end, for_plan=None):
        """Generate monthly invoice for a billing period.

        Args:
            account_id: Account UUID
            period_start: Unix timestamp for period start
            period_end: Unix timestamp for period end
            for_plan: Optional plan id this invoice pays for. When set, paying
                the invoice activates this plan (subscription purchase). When
                None, the account's current plan is billed (renewal).

        Returns:
            dict with invoice details
        """
        with self._lock:
            conn = self._conn()
            account = conn.execute(
                "SELECT id, api_key_prefix, plan, monthly_quota_tokens "
                "FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()

            if not account:
                conn.close()
                return {"error": "Account not found"}

            billed_plan = for_plan or account[2]
            plan_config = PLANS.get(billed_plan, PLANS["free"])
            base_amount_idr = plan_config["monthly_price_idr"] or 0

            # Calculate overage charges
            txn_row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN type='overage' THEN amount_idr ELSE 0 END), 0) "
                "FROM transactions WHERE account_id=? AND timestamp>=? AND timestamp<?",
                (account_id, period_start, period_end),
            ).fetchone()
            overage_amount_idr = txn_row[0]

            total_idr = base_amount_idr + overage_amount_idr
            invoice_id = str(uuid.uuid4())

            conn.execute(
                "INSERT INTO invoices (id, account_id, period_start, period_end, "
                "base_amount_idr, overage_amount_idr, total_idr, status, plan) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (invoice_id, account_id, period_start, period_end,
                 base_amount_idr, overage_amount_idr, total_idr, "pending", billed_plan),
            )
            conn.commit()
            conn.close()

        logger.info(
            "Invoice generated: %s for %s — Rp %s (plan=%s)",
            invoice_id[:8], account[1], f"{total_idr:,}", billed_plan,
        )
        return {
            "id": invoice_id,
            "account_id": account_id,
            "period_start": period_start,
            "period_end": period_end,
            "base_amount_idr": base_amount_idr,
            "overage_amount_idr": overage_amount_idr,
            "total_idr": total_idr,
            "plan": billed_plan,
            "status": "pending",
        }

    def create_subscription_invoice(self, api_key, plan):
        """Create an invoice for purchasing/upgrading to a plan.

        The invoice covers the plan for the current billing period. Paying it
        (via Midtrans webhook) activates the plan for the account.

        Args:
            api_key: The account's API key
            plan: Plan id to purchase (starter, pro, business)

        Returns:
            dict with invoice details, or {"error": ...}
        """
        if plan not in PLANS:
            return {"error": f"Unknown plan: {plan}"}
        if plan == "free":
            return {"error": "Free plan does not require payment"}
        if PLANS[plan]["monthly_price_idr"] is None:
            return {"error": "Enterprise plan requires contacting sales"}

        account = self._get_account(api_key)
        if not account:
            self.create_account(api_key, "free")
            account = self._get_account(api_key)

        return self.generate_invoice(account["id"], _cycle_start(), _cycle_end(), for_plan=plan)

    def get_invoices(self, account_id):
        """List all invoices for an account."""
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT id, period_start, period_end, base_amount_idr, "
                "overage_amount_idr, total_idr, status, paid_at, midtrans_order_id, "
                "COALESCE(plan, '') "
                "FROM invoices WHERE account_id=? ORDER BY period_start DESC",
                (account_id,),
            ).fetchall()
            conn.close()

        return [
            {
                "id": r[0],
                "period_start": r[1],
                "period_end": r[2],
                "base_amount_idr": r[3],
                "overage_amount_idr": r[4],
                "total_idr": r[5],
                "status": r[6],
                "paid_at": r[7],
                "midtrans_order_id": r[8],
                "plan": r[9],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Midtrans Payment Integration
    # ------------------------------------------------------------------

    def create_midtrans_charge(self, invoice_id, amount_idr):
        """Create a Midtrans Snap payment charge.

        Supports: QRIS, bank transfer (BCA, Mandiri, BNI, BRI), GoPay, OVO, DANA.

        Args:
            invoice_id: Invoice UUID
            amount_idr: Amount in IDR

        Returns:
            dict with payment URL and details
        """
        if not MIDTRANS_SERVER_KEY:
            return {"error": "Midtrans server key not configured"}

        order_id = f"OPSORA-{invoice_id[:8].upper()}-{int(_now())}"

        payload = {
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount_idr,
            },
            "customer_details": {
                "first_name": "Opsora",
                "email": "billing@opsora.id",
            },
            "item_details": [
                {
                    "id": "opsora-subscription",
                    "price": amount_idr,
                    "quantity": 1,
                    "name": "Opsora API Subscription",
                }
            ],
            "enabled_payments": [
                "gopay", "shopeepay", "qris",
                "bca_va", "bni_va", "bri_va", "mandiri_va",
                "bank_transfer", "other_va",
            ],
        }

        auth_string = b64encode(
            f"{MIDTRANS_SERVER_KEY}:".encode()
        ).decode()

        url = f"{MIDTRANS_BASE}/transactions"
        data = json.dumps(payload).encode()

        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Basic {auth_string}")

        try:
            resp = urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())

            # Update invoice with Midtrans order ID
            with self._lock:
                conn = self._conn()
                conn.execute(
                    "UPDATE invoices SET midtrans_order_id=? WHERE id=?",
                    (order_id, invoice_id),
                )
                conn.commit()
                conn.close()

            return {
                "order_id": order_id,
                "token": result.get("token"),
                "redirect_url": result.get("redirect_url"),
                "amount_idr": amount_idr,
            }

        except HTTPError as e:
            try:
                err_body = json.loads(e.read().decode())
            except Exception:
                err_body = {"message": str(e)}
            logger.error("Midtrans error: %s", err_body)
            return {"error": "Payment creation failed", "details": err_body}

    def handle_midtrans_notification(self, notification):
        """Process Midtrans webhook notification.

        Verifies signature and updates invoice status.

        Args:
            notification: dict from Midtrans webhook

        Returns:
            dict with processing result
        """
        if not MIDTRANS_SERVER_KEY:
            return {"error": "Midtrans not configured"}

        # Verify signature
        order_id = notification.get("order_id", "")
        status_code = notification.get("status_code", "")
        gross_amount = notification.get("gross_amount", "")
        signature_key = notification.get("signature_key", "")

        raw = f"{order_id}{status_code}{gross_amount}{MIDTRANS_SERVER_KEY}"
        expected_sig = hashlib.sha512(raw.encode()).hexdigest()

        if signature_key != expected_sig:
            logger.warning("Midtrans signature mismatch for order %s", order_id)
            return {"error": "Invalid signature"}

        transaction_status = notification.get("transaction_status", "")

        # Map Midtrans status to our status
        status_map = {
            "capture": "paid",
            "settlement": "paid",
            "pending": "pending",
            "deny": "failed",
            "cancel": "cancelled",
            "expire": "expired",
            "failure": "failed",
        }
        new_status = status_map.get(transaction_status, "unknown")

        # Update invoice
        with self._lock:
            conn = self._conn()
            invoice = conn.execute(
                "SELECT id, account_id, total_idr, COALESCE(plan, '') "
                "FROM invoices WHERE midtrans_order_id=?",
                (order_id,),
            ).fetchone()

            if not invoice:
                conn.close()
                return {"error": "Invoice not found"}

            invoice_id, account_id, total_idr, invoice_plan = invoice
            paid_at = _now() if new_status == "paid" else None
            conn.execute(
                "UPDATE invoices SET status=?, paid_at=? WHERE id=?",
                (new_status, paid_at, invoice_id),
            )

            # If paid, record the payment and activate the purchased plan.
            # (Previously this only credited balance_idr and never changed
            # the account plan — customers paid but stayed on Free.)
            if new_status == "paid":
                conn.execute(
                    "INSERT INTO transactions (id, account_id, timestamp, type, "
                    "amount_idr, tokens, model, description) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), account_id, _now(), "payment",
                     total_idr, 0, "", f"Payment via Midtrans: {order_id}"),
                )

            conn.commit()
            conn.close()

        if new_status == "paid" and invoice_plan:
            activated = self._activate_paid_plan(account_id, invoice_plan, order_id)
        else:
            activated = None

        logger.info("Midtrans notification: order=%s status=%s plan=%s",
                    order_id, new_status, activated or "-")
        result = {
            "order_id": order_id,
            "status": new_status,
            "invoice_id": invoice_id,
        }
        if activated:
            result["activated_plan"] = activated
        return result

    def _activate_paid_plan(self, account_id, plan, order_id=""):
        """Activate a paid plan for an account after successful payment.

        Starts a fresh billing cycle now with the plan's full quota.

        Returns:
            plan id when activated, None when nothing changed.
        """
        if plan not in PLANS or plan == "free":
            return None
        plan_config = PLANS[plan]
        quota = plan_config["monthly_quota_tokens"] or 0

        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT plan FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not row:
                conn.close()
                return None
            old_plan = row[0]

            conn.execute(
                "UPDATE accounts SET plan=?, billing_cycle_start=?, "
                "monthly_quota_tokens=?, tokens_used_this_cycle=0, status='active' "
                "WHERE id=?",
                (plan, _now(), quota, account_id),
            )
            conn.execute(
                "INSERT INTO transactions (id, account_id, timestamp, type, "
                "amount_idr, tokens, model, description) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), account_id, _now(), "plan_activation",
                 0, quota, "",
                 f"Plan activated: {old_plan} → {plan} (order {order_id})"),
            )
            conn.commit()
            conn.close()

        logger.info("Plan activated for account %s: %s → %s",
                    account_id[:8], old_plan, plan)
        return plan

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def check_alerts(self, api_key):
        """Check if usage alerts should be triggered.

        Returns:
            list of alert dicts with level, message
        """
        account = self._get_account(api_key)
        if not account:
            return []

        quota = account["monthly_quota_tokens"]
        if quota <= 0:
            return []

        used = account["tokens_used_this_cycle"]
        percentage = (used / quota) * 100

        alerts = []
        if percentage >= 100:
            plan_config = PLANS.get(account["plan"], PLANS["free"])
            if not plan_config["overage_allowed"]:
                alerts.append({
                    "level": "critical",
                    "message": "Quota exhausted. Requests will be blocked.",
                    "percentage": round(percentage, 1),
                    "action": "Upgrade your plan to continue using the API.",
                })
            else:
                alerts.append({
                    "level": "warning",
                    "message": "Quota exceeded. Overage charges apply.",
                    "percentage": round(percentage, 1),
                    "action": "Monitor usage to control costs.",
                })
        elif percentage >= 95:
            alerts.append({
                "level": "critical",
                "message": f"95% quota used ({used:,}/{quota:,} tokens).",
                "percentage": round(percentage, 1),
                "action": "Consider upgrading your plan.",
            })
        elif percentage >= 80:
            alerts.append({
                "level": "warning",
                "message": f"80% quota used ({used:,}/{quota:,} tokens).",
                "percentage": round(percentage, 1),
                "action": "Monitor your usage closely.",
            })

        return alerts

    # ------------------------------------------------------------------
    # Cycle Management
    # ------------------------------------------------------------------

    def reset_cycle(self, api_key):
        """Reset billing cycle for a new month.

        Resets tokens_used_this_cycle and updates billing_cycle_start.
        """
        account = self._get_account(api_key)
        if not account:
            return

        new_cycle_start = _cycle_start()
        plan_config = PLANS.get(account["plan"], PLANS["free"])
        new_quota = plan_config["monthly_quota_tokens"] or 0

        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE accounts SET billing_cycle_start=?, tokens_used_this_cycle=0, "
                "monthly_quota_tokens=? WHERE id=?",
                (new_cycle_start, new_quota, account["id"]),
            )
            conn.commit()
            conn.close()

        logger.info("Billing cycle reset for %s", account["api_key_prefix"])
