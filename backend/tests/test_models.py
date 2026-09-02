"""Tests for backend/app/models.py -- no DB needed, plain dict fixtures."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.models import Discrepancy, Order, Payment, ReconciliationRun


def test_order_from_record_round_trips_all_columns():
    record = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "order_id": "ORD-1001",
        "order_id_norm": "ord1001",
        "order_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "customer_email": "buyer@example.com",
        "currency": "USD",
        "gross_amount": Decimal("100.00"),
        "discount": Decimal("10.00"),
        "net_amount": Decimal("90.00"),
        "status": "completed",
        "upload_batch_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }

    order = Order.from_record(record)

    assert isinstance(order, Order)
    for key, value in record.items():
        assert getattr(order, key) == value


def test_order_tolerates_nullable_columns():
    record = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "order_id": "ORD-1002",
        "order_id_norm": "ord1002",
        "order_date": None,
        "customer_email": None,
        "currency": "USD",
        "gross_amount": Decimal("50.00"),
        "discount": Decimal("0"),
        "net_amount": Decimal("50.00"),
        "status": "pending",
        "upload_batch_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }

    order = Order.from_record(record)

    assert order.order_date is None
    assert order.customer_email is None


def test_payment_from_record_round_trips_all_columns():
    record = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "transaction_ref": "TXN-1",
        "processed_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
        "order_reference": "ORD-1001",
        "order_reference_norm": "ord1001",
        "currency": "USD",
        "amount": Decimal("90.00"),
        "fee": Decimal("2.50"),
        "net_settled": Decimal("87.50"),
        "type": "charge",
        "status": "settled",
        "upload_batch_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
    }

    payment = Payment.from_record(record)

    assert isinstance(payment, Payment)
    for key, value in record.items():
        assert getattr(payment, key) == value


def test_payment_tolerates_null_net_settled_and_processed_at():
    record = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "transaction_ref": "TXN-2",
        "processed_at": None,
        "order_reference": "ORD-1002",
        "order_reference_norm": "ord1002",
        "currency": "USD",
        "amount": Decimal("50.00"),
        "fee": Decimal("0"),
        "net_settled": None,
        "type": "charge",
        "status": "pending",
        "upload_batch_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
    }

    payment = Payment.from_record(record)

    assert payment.processed_at is None
    assert payment.net_settled is None


def test_reconciliation_run_from_record():
    record = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "created_at": datetime(2026, 1, 4, tzinfo=timezone.utc),
        "orders_count": 186,
        "payments_count": 188,
        "total_reconciled_value": Decimal("12345.67"),
        "total_disputed_value": Decimal("100.00"),
        "money_at_risk": Decimal("75.00"),
        "status": "complete",
    }

    run = ReconciliationRun.from_record(record)

    assert isinstance(run, ReconciliationRun)
    for key, value in record.items():
        assert getattr(run, key) == value


def test_discrepancy_from_record_with_nullable_fields():
    record = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "type": "MISSING_PAYMENT",
        "order_id": "ORD-1003",
        "payment_ref": None,
        "order_amount": Decimal("75.00"),
        "payment_amount": None,
        "currency_order": "USD",
        "currency_payment": None,
        "difference": None,
        "detail": {"note": "no matching payment found"},
        "explanation": None,
        "explained_at": None,
    }

    discrepancy = Discrepancy.from_record(record)

    assert isinstance(discrepancy, Discrepancy)
    for key, value in record.items():
        assert getattr(discrepancy, key) == value
