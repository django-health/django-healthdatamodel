"""
Test utilities for the healthdatamodel app.

Fixture data should be created through the ingest API (via the helpers
below) rather than ``Record.objects.create(...)``: from v1.0, reads are
served from compact storage that only the ingest path keeps in sync, so
records created directly on the model will be invisible to the query API.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from healthdatamodel.constants import ConnectionStatus, DataSource
from healthdatamodel.ingest import ingest_records
from healthdatamodel.models import WearableConnection
from healthdatamodel.query import ActivityMetric
from healthdatamodel.schemas import RecordInput

__all__ = [
    "DEFAULT_RECORD_START",
    "create_record",
    "create_records",
    "record_input",
    "set_customer_device",
]

#: ``startDate`` used by :func:`record_input` when none is given.
DEFAULT_RECORD_START = datetime(2025, 1, 6, tzinfo=timezone.utc)  # a Monday

_DEFAULT_DURATION = timedelta(minutes=15)


def set_customer_device(
    customer: Any,
    data_source: str,
    device_brand: str = "",
    preferred_for_sleep: bool = False,
) -> WearableConnection:
    """Create or update a WearableConnection for the given customer.

    Operates directly on the WearableConnection model — does **not**
    depend on any property/setter on the customer model.
    """
    defaults: dict[str, Any] = {
        "status": ConnectionStatus.ACTIVE,
    }
    if device_brand:
        defaults["device_brand"] = device_brand
    if preferred_for_sleep:
        defaults["preferred_for_sleep"] = True
    conn, _ = WearableConnection.objects.update_or_create(
        customer=customer,
        data_source=data_source,
        defaults=defaults,
    )
    # Deactivate other active connections so this one is the primary.
    WearableConnection.objects.filter(
        customer=customer,
        status=ConnectionStatus.ACTIVE,
    ).exclude(pk=conn.pk).update(status=ConnectionStatus.DISCONNECTED)
    return conn


def record_input(**overrides: Any) -> RecordInput:
    """Build a :class:`~healthdatamodel.schemas.RecordInput` with sensible defaults.

    Defaults: a 15-minute step-count record with value ``"0"`` and no unit,
    starting at :data:`DEFAULT_RECORD_START`, ``sourceName="test"``,
    ``creationDate=now``.
    ``endDate`` defaults to ``startDate + 15 min`` (so overriding only
    ``startDate`` still gives a valid interval).  Any ``RecordInput`` field
    can be overridden; numeric ``value`` is accepted and stringified.
    """
    start = overrides.pop("startDate", DEFAULT_RECORD_START)
    fields: dict[str, Any] = {
        "startDate": start,
        "endDate": overrides.pop("endDate", start + _DEFAULT_DURATION),
        "creationDate": datetime.now(timezone.utc),
        "sourceName": "test",
        "value": "0",
        "unit": None,
        "type": ActivityMetric.STEPS.value,
    }
    fields.update(overrides)
    return RecordInput(**fields)


def create_records(
    customer: Any,
    records: Iterable[RecordInput | Mapping[str, Any]],
    *,
    source: str = DataSource.APPLE_HEALTH,
    admin_create_date: datetime | None = None,
) -> list[RecordInput]:
    """Create fixture records for *customer* through :func:`ingest_records`.

    Each item is either a ready ``RecordInput`` or a mapping of
    ``RecordInput`` fields that is passed through :func:`record_input` (so
    omitted fields get defaults).  A mapping may also carry its own
    ``"source"`` and/or ``"admin_create_date"``, overriding the call-level
    arguments for that record — handy when converting
    ``Record.objects.create(...)`` calls that set them per row.  A
    ``"customer"`` key, if present, is ignored (``customer`` wins).

    Records are ingested in groups of equal ``(source, admin_create_date)``,
    preserving their order within each group.  ``admin_create_date`` defaults
    to one shared ``now()`` for the whole call.

    Returns
    -------
    list[RecordInput]
        The ingested records, in input order.
    """
    default_acd = admin_create_date or datetime.now(timezone.utc)
    groups: dict[tuple[str, datetime], list[RecordInput]] = {}
    built: list[RecordInput] = []
    for item in records:
        row_source, row_acd = source, default_acd
        if isinstance(item, RecordInput):
            rec = item
        else:
            fields = dict(item)
            fields.pop("customer", None)
            row_source = fields.pop("source", source)
            row_acd = fields.pop("admin_create_date", None) or default_acd
            rec = record_input(**fields)
        groups.setdefault((str(row_source), row_acd), []).append(rec)
        built.append(rec)
    for (grp_source, grp_acd), grp in groups.items():
        ingest_records(customer, grp, source=grp_source, admin_create_date=grp_acd)
    return built


def create_record(customer: Any, **fields: Any) -> RecordInput:
    """Create a single fixture record; a drop-in for ``Record.objects.create``.

    ``create_record(customer, startDate=..., type=..., value=20,
    source=DataSource.FITBIT, admin_create_date=ts)`` — accepts the same
    keyword fields as a ``Record`` (minus ``customer``), with the defaults of
    :func:`record_input` and :func:`create_records`.
    """
    return create_records(customer, [fields])[0]
