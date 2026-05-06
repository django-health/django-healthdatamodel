"""Tests for healthdatamodel admin configurations."""

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory

from healthdatamodel.admin import WearableConnectionAdmin
from healthdatamodel.models import WearableConnection

pytestmark = pytest.mark.django_db


class TestWearableConnectionAdmin:
    """Test WearableConnectionAdmin search functionality."""

    def test_search_fields_does_not_raise_error(self, default_customer):
        """
        Test that searching in WearableConnection admin doesn't raise FieldError.

        This verifies the fix for the icontains ForeignKey lookup error that
        appeared in NewRelic when searching by customer name.
        """
        WearableConnection.objects.create(
            customer=default_customer,
            data_source="health_connect",
            device_brand="samsung",
            status="active",
        )

        admin_instance = WearableConnectionAdmin(WearableConnection, site)
        factory = RequestFactory()
        request = factory.get(
            "/admin/healthdatamodel/wearableconnection/", {"q": "Default"}
        )

        queryset = WearableConnection.objects.all()
        filtered_queryset, use_distinct = admin_instance.get_search_results(
            request, queryset, "Default"
        )

        assert filtered_queryset is not None

    def test_search_by_customer_fields(self, default_customer):
        """Test that search works across customer fields."""
        connection = WearableConnection.objects.create(
            customer=default_customer,
            data_source="health_connect",
            device_brand="samsung",
            status="active",
        )

        admin_instance = WearableConnectionAdmin(WearableConnection, site)
        factory = RequestFactory()
        queryset = WearableConnection.objects.all()

        request = factory.get("/admin/", {"q": "Default"})
        filtered, _ = admin_instance.get_search_results(request, queryset, "Default")
        assert connection in filtered

        request = factory.get("/admin/", {"q": "default@example.com"})
        filtered, _ = admin_instance.get_search_results(
            request, queryset, "default@example.com"
        )
        assert connection in filtered
