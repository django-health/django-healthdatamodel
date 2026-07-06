from django.conf import settings
from django.db import models

from healthdatamodel.constants import ConnectionStatus, DataSource, DeviceBrand


class DataSourceRanking(models.Model):
    """
    Store the ranking of data sources for a customer.

    This is only used when we detect more than 1 data source for a customer
    within a week, and we need to pick one within smaller time windows.

    Set initially in user_status.py in the /user-v2 calls,
    and subsequently updated in the /data-source-update calls.
    """

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    dataSource = models.CharField(
        max_length=100, null=True, blank=True, choices=DataSource.choices
    )
    rank = models.IntegerField()

    def __str__(self):
        return f"{self.customer} {self.dataSource} {self.rank}"

    # these would work fine
    # but we don't need them because we're not using
    # bulk_create with update_conflicts=True
    # class Meta:
    #     constraints = [
    #         models.UniqueConstraint(
    #             fields=["customer", "dataSource"],
    #             name="unique_customer_dataSource",
    #         )
    #     ]
    #
    # class Meta:
    #     constraints = [
    #         models.UniqueConstraint(
    #             fields=["customer", "rank"],
    #             name="unique_customer_rank",
    #         )
    #     ]


class Workout(models.Model):
    """
    Store workouts in a schema inspired by the internal
    storage format of Apple HealthKit.
    """

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    startDate = models.DateTimeField()
    endDate = models.DateTimeField()
    creationDate = models.DateTimeField()
    sourceVersion = models.CharField(null=True, blank=True, max_length=200)
    sourceName = models.CharField(max_length=200)
    # one of: Apple HealthKit, Fitbit, GF, AHC
    # could make this list formal
    source = models.CharField(max_length=200)
    device = models.CharField(null=True, blank=True, max_length=200)
    durationUnit = models.CharField(max_length=200)
    duration = models.IntegerField()
    workoutActivityType = models.CharField(max_length=200)
    admin_create_date = models.DateTimeField(auto_now_add=True)


class WorkoutMetadataEntry(models.Model):
    """
    Store metadata for workouts in a schema inspired by the internal
    storage format of Apple HealthKit.
    """

    class Meta:
        verbose_name_plural = "WorkoutMetadataEntry"

    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, unique=False)
    value = models.CharField(max_length=200)
    key = models.CharField(max_length=200)


class Record(models.Model):
    """
    Store records in a schema inspired by the internal
    storage format of Apple HealthKit.

    Each record has a type, value, unit, time range, and other details.

    Time ranges are arbitrary.
    Our app uses specifically 15 minute and 1 day entries for activity,
    and arbritrary start/end times for sleep events.
    For more details on these intervals and
    the definition of our LCRO scheme.

    The type should be the specific name from Apple HealthKit.
    """

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    startDate = models.DateTimeField()
    endDate = models.DateTimeField()
    creationDate = models.DateTimeField()
    sourceVersion = models.CharField(null=True, blank=True, max_length=200)
    sourceName = models.CharField(max_length=200)
    # one of: Apple HealthKit, Fitbit, GF, AHC
    # could make this list formal
    source = models.CharField(max_length=200)
    # should be able to convert to float
    value = models.CharField(max_length=200)
    unit = models.CharField(null=True, blank=True, max_length=200)
    # e.g., ActiveCalories, BasalCalories, SleepDeep, SleepLight, SleepAwake
    type = models.CharField(max_length=200)
    device = models.CharField(null=True, blank=True, max_length=200)
    admin_create_date = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(
                fields=["customer", "type", "startDate"],
                name="record_customer_type_start_idx",
            ),
        ]

    def __str__(self):
        return f"{self.customer} {self.type} {self.startDate=} {self.endDate=} {self.creationDate=} {self.admin_create_date=} {self.value=} {self.unit=}"


class WearableConnection(models.Model):
    """
    Track wearable device connections for a customer.

    A customer can have multiple active connections simultaneously
    (e.g., Fitbit for activity + Apple Watch for sleep).  Each row
    represents a single data-source / device-brand pairing and its
    current status.

    This replaces the single ``device`` / ``device_brand`` /
    ``preferred_sleep_device`` fields that previously lived on the
    Customer model, giving us:
      - one-to-many connections per customer,
      - an explicit connected / disconnected lifecycle,
      - a ``preferred_for_sleep`` flag so sleep-source ranking is
        stored alongside the connection rather than on the user.
    """

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wearable_connections",
    )
    data_source = models.CharField(
        max_length=100,
        choices=DataSource.choices,
        help_text="The data pipeline this connection feeds (apple_health, fitbit, health_connect).",
    )
    device_brand = models.CharField(
        max_length=100,
        blank=True,
        default="",
        choices=DeviceBrand.choices,
        help_text="Brand of the physical device (apple, fitbit, garmin …).",
    )
    status = models.CharField(
        max_length=20,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.ACTIVE,
    )
    preferred_for_sleep = models.BooleanField(
        default=False,
        help_text="If True, this connection's device_brand is preferred when choosing sleep data.",
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "data_source"],
                name="unique_customer_data_source",
            ),
        ]
        ordering = ["connected_at"]

    def __str__(self) -> str:
        return f"{self.customer} — {self.data_source} ({self.device_brand}) [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status == ConnectionStatus.ACTIVE


# Need to keep these arround for the old migrations
class TruncatingCharField(models.CharField):
    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value:
            return value[: self.max_length]
        return value
