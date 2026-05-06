import csv
from datetime import timedelta

from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class DateRangeFilter(admin.SimpleListFilter):
    """
    Reusable date/datetime range filter.

    Subclass and set `title`, `parameter_name`, and `field_name`.
    If the field is a DateField (not DateTimeField), set `is_date_field = True`
    to avoid the unsupported `__date` lookup on plain DateFields.
    """

    title = "date range"
    parameter_name = "date_range"
    field_name = "created_at"
    is_date_field = False

    def lookups(self, request, model_admin):
        return [
            ("today", "Today"),
            ("7d", "Last 7 days"),
            ("30d", "Last 30 days"),
            ("90d", "Last 90 days"),
        ]

    def queryset(self, request, queryset):
        val = self.value()
        if not val:
            return queryset

        now = timezone.now()
        today = now.date()

        if self.is_date_field:
            if val == "today":
                return queryset.filter(**{self.field_name: today})
            days_map = {"7d": 7, "30d": 30, "90d": 90}
            if val in days_map:
                cutoff = today - timedelta(days=days_map[val])
                return queryset.filter(**{f"{self.field_name}__gte": cutoff})
        else:
            if val == "today":
                return queryset.filter(**{f"{self.field_name}__date": today})
            days_map = {"7d": 7, "30d": 30, "90d": 90}
            if val in days_map:
                cutoff = now - timedelta(days=days_map[val])
                return queryset.filter(**{f"{self.field_name}__gte": cutoff})

        return queryset


class BasePermissionMixin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


class ReadonlyMixin(BasePermissionMixin):
    pass


class ExportCsvMixin:
    def export_as_csv(self, request, queryset):
        meta = self.model._meta
        field_names = [field.name for field in meta.fields]

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f"attachment; filename={meta}.csv"
        writer = csv.writer(response)

        writer.writerow(field_names)
        for obj in queryset:
            writer.writerow([getattr(obj, field) for field in field_names])

        return response

    export_as_csv.short_description = "Export as CSV"  # type: ignore


# ---------------------------------------------------------------------------
# Admin classes — registration is left to the host project (see demo/admin.py)
# ---------------------------------------------------------------------------


class StartDateFilter(DateRangeFilter):
    title = "Start Date"
    parameter_name = "start_date_range"
    field_name = "startDate"


class WorkoutAdmin(ReadonlyMixin):
    list_display = [
        "customer_id",
        "customer_email",
        "workoutActivityType",
        "startDate",
        "endDate",
        "creationDate",
        "admin_create_date",
    ]
    search_fields = [
        "workoutActivityType",
        "customer__email",
    ]
    search_help_text = "Search by activity type or customer email"
    list_filter = ["workoutActivityType", StartDateFilter]
    ordering = ["-startDate"]
    list_per_page = 25
    list_select_related = ("customer",)
    show_full_result_count = False
    empty_value_display = "—"
    raw_id_fields = ("customer",)

    @admin.display(description="Customer ID", ordering="customer__id")
    def customer_id(self, obj):
        return obj.customer.pk if obj.customer else "N/A"

    @admin.display(description="Customer Email", ordering="customer__email")
    def customer_email(self, obj):
        return obj.customer.email if obj.customer else "N/A"


class RecordAdmin(ReadonlyMixin):
    list_display = [
        "customer_id",
        "customer_email",
        "type",
        "startDate",
        "endDate",
        "admin_create_date",
    ]
    search_fields = [
        "type",
        "customer__email",
    ]
    search_help_text = "Search by record type or customer email"
    list_filter = ["type", StartDateFilter]
    ordering = ["-startDate"]
    list_per_page = 25
    list_select_related = ("customer",)
    show_full_result_count = False
    empty_value_display = "—"
    raw_id_fields = ("customer",)

    @admin.display(description="Customer ID", ordering="customer__id")
    def customer_id(self, obj):
        return obj.customer.pk if obj.customer else "N/A"

    @admin.display(description="Customer Email", ordering="customer__email")
    def customer_email(self, obj):
        return obj.customer.email if obj.customer else "N/A"


class DataSourceRankingAdmin(admin.ModelAdmin, ExportCsvMixin):
    list_display = ("customer", "dataSource", "rank")
    search_fields = ["customer__email"]
    search_help_text = "Search by customer email"
    list_filter = ("dataSource",)
    ordering = ("customer", "rank")
    list_per_page = 25
    list_select_related = ("customer",)
    raw_id_fields = ("customer",)
    actions = ["export_as_csv"]


class WorkoutMetadataEntryAdmin(admin.ModelAdmin):
    list_display = ("workout", "value")
    search_fields = ("value",)
    search_help_text = "Search by metadata value"
    list_per_page = 25
    raw_id_fields = ("workout",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class WearableConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "data_source",
        "device_brand",
        "status",
        "preferred_for_sleep",
        "connected_at",
        "last_synced_at",
    )
    search_fields = [
        "customer__email",
        "customer__first_name",
        "customer__last_name",
    ]
    search_help_text = "Search by customer email or name"
    list_filter = ("data_source", "device_brand", "status")
    ordering = ("-connected_at",)
    list_per_page = 25
    list_select_related = ("customer",)
    raw_id_fields = ("customer",)
