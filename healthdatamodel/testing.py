"""
Test utilities for the healthdatamodel app.
"""

from __future__ import annotations

from typing import Any

from healthdatamodel.constants import ConnectionStatus
from healthdatamodel.models import WearableConnection


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
