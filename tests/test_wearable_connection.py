from datetime import datetime, timezone

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from healthdatamodel.constants import ConnectionStatus, DataSource, DeviceBrand
from healthdatamodel.models import WearableConnection

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="wc-test-1")


@pytest.fixture
def second_user():
    return User.objects.create_user(username="wc-test-2")


class TestWearableConnectionCreate:
    def test_create_basic(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
            device_brand=DeviceBrand.APPLE,
        )
        assert conn.pk is not None
        assert conn.data_source == "apple_health"
        assert conn.device_brand == "apple"
        assert conn.status == ConnectionStatus.ACTIVE
        assert conn.is_active is True
        assert conn.preferred_for_sleep is False
        assert conn.connected_at is not None
        assert conn.disconnected_at is None

    def test_str(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
            device_brand=DeviceBrand.FITBIT,
        )
        s = str(conn)
        assert "fitbit" in s
        assert "active" in s

    def test_default_status_is_active(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.HEALTH_CONNECT,
        )
        assert conn.status == ConnectionStatus.ACTIVE

    def test_device_brand_optional(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
        )
        assert conn.device_brand == ""


class TestWearableConnectionMultiple:
    def test_multiple_connections_per_customer(self, user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
            device_brand=DeviceBrand.APPLE,
        )
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
            device_brand=DeviceBrand.FITBIT,
        )
        assert WearableConnection.objects.filter(customer=user).count() == 2

    def test_unique_customer_data_source(self, user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
        )
        with pytest.raises(IntegrityError):
            WearableConnection.objects.create(
                customer=user,
                data_source=DataSource.APPLE_HEALTH,
            )

    def test_same_source_different_customers(self, user, second_user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
        )
        WearableConnection.objects.create(
            customer=second_user,
            data_source=DataSource.FITBIT,
        )
        assert (
            WearableConnection.objects.filter(data_source=DataSource.FITBIT).count()
            == 2
        )


class TestWearableConnectionLifecycle:
    def test_disconnect(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
            device_brand=DeviceBrand.FITBIT,
        )
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        conn.status = ConnectionStatus.DISCONNECTED
        conn.disconnected_at = now
        conn.save()

        conn.refresh_from_db()
        assert conn.is_active is False
        assert conn.disconnected_at == now

    def test_cascade_delete(self, user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
        )
        assert WearableConnection.objects.count() == 1
        user.delete()
        assert WearableConnection.objects.count() == 0


class TestPreferredForSleep:
    def test_set_preferred(self, user):
        conn = WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
            device_brand=DeviceBrand.APPLE,
            preferred_for_sleep=True,
        )
        assert conn.preferred_for_sleep is True

    def test_multiple_with_one_preferred(self, user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.APPLE_HEALTH,
            device_brand=DeviceBrand.APPLE,
            preferred_for_sleep=True,
        )
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
            device_brand=DeviceBrand.FITBIT,
            preferred_for_sleep=False,
        )
        preferred = WearableConnection.objects.filter(
            customer=user, preferred_for_sleep=True
        )
        assert preferred.count() == 1
        assert preferred.first().data_source == DataSource.APPLE_HEALTH


class TestActiveFor:
    def test_none_when_no_connections(self, user):
        assert WearableConnection.active_for(user) is None

    def test_none_when_all_disconnected(self, user):
        WearableConnection.objects.create(
            customer=user,
            data_source=DataSource.FITBIT,
            status=ConnectionStatus.DISCONNECTED,
        )
        assert WearableConnection.active_for(user) is None

    def test_most_recently_connected_active_wins(self, user):
        older = WearableConnection.objects.create(
            customer=user, data_source=DataSource.FITBIT
        )
        newer = WearableConnection.objects.create(
            customer=user, data_source=DataSource.APPLE_HEALTH
        )
        # auto_now_add stamps both in the same test transaction; force ordering
        WearableConnection.objects.filter(pk=older.pk).update(
            connected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        WearableConnection.objects.filter(pk=newer.pk).update(
            connected_at=datetime(2026, 2, 1, tzinfo=timezone.utc)
        )
        assert WearableConnection.active_for(user).pk == newer.pk


class TestActivate:
    def test_creates_active_row(self, user):
        conn = WearableConnection.activate(user, DataSource.APPLE_HEALTH)
        assert conn.status == ConnectionStatus.ACTIVE
        assert conn.device_brand == ""
        active = WearableConnection.objects.filter(
            customer=user, status=ConnectionStatus.ACTIVE
        )
        assert active.count() == 1

    def test_explicit_brand_wins(self, user):
        conn = WearableConnection.activate(
            user, DataSource.APPLE_HEALTH, device_brand=DeviceBrand.GARMIN
        )
        assert conn.device_brand == DeviceBrand.GARMIN

    def test_direct_source_implies_brand(self, user):
        conn = WearableConnection.activate(user, DataSource.FITBIT)
        assert conn.device_brand == DeviceBrand.FITBIT

    def test_deactivates_connection(self, user):
        """
        Hit both deactivation paths...
        """
        conn1 = WearableConnection.activate(user, DataSource.APPLE_HEALTH)
        WearableConnection.activate(user, DataSource.FITBIT)
        active = WearableConnection.objects.filter(
            customer=user, status=ConnectionStatus.ACTIVE
        )
        assert active.count() == 2
        WearableConnection.deactivate_data_source(user, DataSource.FITBIT)
        active = WearableConnection.objects.filter(
            customer=user, status=ConnectionStatus.ACTIVE
        )
        assert active.count() == 1
        conn1.deactivate()
        active = WearableConnection.objects.filter(
            customer=user, status=ConnectionStatus.ACTIVE
        )
        assert active.count() == 0

    def test_reactivate_clears_disconnected_at_and_keeps_brand(self, user):
        WearableConnection.activate(
            user, DataSource.APPLE_HEALTH, device_brand=DeviceBrand.OURA
        )
        WearableConnection.activate(user, DataSource.FITBIT)
        conn = WearableConnection.activate(user, DataSource.APPLE_HEALTH)
        assert conn.status == ConnectionStatus.ACTIVE
        assert conn.disconnected_at is None
        assert conn.device_brand == DeviceBrand.OURA

    def test_aggregator_inherits_brand_from_outgoing(self, user):
        """A Garmin user switching apple_health → google_health keeps their brand."""
        WearableConnection.activate(
            user, DataSource.APPLE_HEALTH, device_brand=DeviceBrand.GARMIN
        )
        conn = WearableConnection.activate(user, DataSource.GOOGLE_HEALTH)
        assert conn.device_brand == DeviceBrand.GARMIN

    def test_direct_source_does_not_inherit(self, user):
        WearableConnection.activate(
            user, DataSource.HEALTH_CONNECT, device_brand=DeviceBrand.SAMSUNG
        )
        conn = WearableConnection.activate(user, DataSource.FITBIT)
        assert conn.device_brand == DeviceBrand.FITBIT

    def test_idempotent_on_same_source(self, user):
        first = WearableConnection.activate(
            user, DataSource.APPLE_HEALTH, device_brand=DeviceBrand.APPLE
        )
        again = WearableConnection.activate(user, DataSource.APPLE_HEALTH)
        assert again.pk == first.pk
        assert again.device_brand == DeviceBrand.APPLE
        assert (
            WearableConnection.objects.filter(
                customer=user, status=ConnectionStatus.ACTIVE
            ).count()
            == 1
        )


class TestSetBrand:
    def test_writes_to_active_connection(self, user):
        WearableConnection.activate(user, DataSource.GOOGLE_HEALTH)
        conn = WearableConnection.set_brand(user, DeviceBrand.FITBIT)
        assert conn is not None
        assert conn.data_source == DataSource.GOOGLE_HEALTH
        assert conn.device_brand == DeviceBrand.FITBIT

    def test_none_without_active_connection(self, user):
        assert WearableConnection.set_brand(user, DeviceBrand.APPLE) is None

    def test_brand_set_then_survives_source_switch(self, user):
        """The #2105 scenario: brand picked while on google_health survives switching off it."""
        WearableConnection.activate(user, DataSource.GOOGLE_HEALTH)
        WearableConnection.set_brand(user, DeviceBrand.GARMIN)
        conn = WearableConnection.activate(user, DataSource.APPLE_HEALTH)
        assert conn.device_brand == DeviceBrand.GARMIN


class TestDeviceBrandConstants:
    def test_device_brands(self):
        assert DeviceBrand.APPLE == "apple"
        assert DeviceBrand.FITBIT == "fitbit"
        assert DeviceBrand.GARMIN == "garmin"
        assert DeviceBrand.SAMSUNG == "samsung"

    def test_connection_statuses(self):
        assert ConnectionStatus.ACTIVE == "active"
        assert ConnectionStatus.DISCONNECTED == "disconnected"
