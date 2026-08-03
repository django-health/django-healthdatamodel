from django.db import models


class DataSource(models.TextChoices):
    APPLE_HEALTH = "apple_health", "Apple Health"
    FITBIT = "fitbit", "Fitbit"
    GARMIN = "garmin", "Garmin"
    GOOGLE_HEALTH = "google_health", "Google Health"
    HEALTH_CONNECT = "health_connect", "Health Connect"
    OURA = "oura", "Oura"
    STRAVA = "strava", "Strava"
    WHOOP = "whoop", "WHOOP"


class DeviceBrand(models.TextChoices):
    APPLE = "apple", "Apple"
    SAMSUNG = "samsung", "Samsung"
    FITBIT = "fitbit", "Fitbit"
    GARMIN = "garmin", "Garmin"
    OURA = "oura", "Oura"
    WHOOP = "whoop", "WHOOP"
    DATAJET = "datajet", "DataJet"  # this is for testing


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"


# ---------------------------------------------------------------------------
# Compact storage (SleepDay / ActivityDay)
# ---------------------------------------------------------------------------

#: HK type string for sleep analysis records.  Re-exported by
#: :mod:`healthdatamodel.query` as ``SLEEP_TYPE`` for backwards compatibility.
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

#: UTC hour at which stored sleep days roll over (2 pm).  Compact sleep rows
#: are canonicalised to this boundary; the query API can still serve other
#: ``day_boundary_hour`` values by re-windowing the stored interval pieces.
SLEEP_DAY_BOUNDARY_HOUR = 14

MINUTES_PER_DAY = 1440


class CompactMetric(models.TextChoices):
    """Activity metrics stored in :class:`~healthdatamodel.models.ActivityDay`.

    Values are short codes (not HK type strings) because they repeat once per
    row; see ``HK_TYPE_TO_COMPACT_METRIC`` for the mapping.  Units are
    canonical: kcal for energy, count for steps.
    """

    ACTIVE_KCAL = "active_kcal", "Active energy (kcal)"
    BASAL_KCAL = "basal_kcal", "Basal energy (kcal)"
    STEPS = "steps", "Steps"


#: HK type string (``Record.type`` / ``RecordInput.type``) → compact metric.
HK_TYPE_TO_COMPACT_METRIC = {
    "HKQuantityTypeIdentifierActiveEnergyBurned": CompactMetric.ACTIVE_KCAL,
    "HKQuantityTypeIdentifierBasalEnergyBurned": CompactMetric.BASAL_KCAL,
    "HKQuantityTypeIdentifierStepCount": CompactMetric.STEPS,
}


class SleepStage(models.TextChoices):
    """Sleep stages stored inside :class:`~healthdatamodel.models.SleepDay`
    interval entries.

    All HKCategoryValueSleepAnalysis* values are kept (including awake and
    in-bed) so no information is lost once ``Record`` writes are disabled;
    the query layer only counts ``ASLEEP_STAGES``.
    """

    UNSPECIFIED = "unspecified", "Asleep (unspecified)"
    CORE = "core", "Asleep (core)"
    DEEP = "deep", "Asleep (deep)"
    REM = "rem", "Asleep (REM)"
    AWAKE = "awake", "Awake"
    IN_BED = "in_bed", "In bed"


#: HK sleep value string (``Record.value``) → compact stage.
HK_SLEEP_VALUE_TO_STAGE = {
    "HKCategoryValueSleepAnalysisAsleepUnspecified": SleepStage.UNSPECIFIED,
    "HKCategoryValueSleepAnalysisAsleepCore": SleepStage.CORE,
    "HKCategoryValueSleepAnalysisAsleepDeep": SleepStage.DEEP,
    "HKCategoryValueSleepAnalysisAsleepREM": SleepStage.REM,
    "HKCategoryValueSleepAnalysisAwake": SleepStage.AWAKE,
    "HKCategoryValueSleepAnalysisInBed": SleepStage.IN_BED,
}

#: Stages that count as sleep in the query API (mirrors the legacy
#: ``value__startswith="HKCategoryValueSleepAnalysisAsleep"`` filter).
ASLEEP_STAGES = frozenset(
    {
        SleepStage.UNSPECIFIED,
        SleepStage.CORE,
        SleepStage.DEEP,
        SleepStage.REM,
    }
)
