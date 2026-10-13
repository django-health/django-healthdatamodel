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


# Sources that relay data recorded by a physical device of a possibly
# different brand (an Apple Health connection may be fed by a Garmin watch).
# Only for these does ``device_brand`` carry information the source doesn't.
AGGREGATOR_SOURCES = frozenset(
    {
        DataSource.APPLE_HEALTH,
        DataSource.GOOGLE_HEALTH,
        DataSource.HEALTH_CONNECT,
        DataSource.STRAVA,
    }
)

# Direct-device sources imply the physical brand.
SOURCE_IMPLIED_BRAND = {
    DataSource.FITBIT: DeviceBrand.FITBIT,
    DataSource.GARMIN: DeviceBrand.GARMIN,
    DataSource.OURA: DeviceBrand.OURA,
    DataSource.WHOOP: DeviceBrand.WHOOP,
}


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
#:
#: Use :func:`sleep_stage_for_value` rather than this map directly: it also
#: maps unknown ``...Asleep*`` values to ``UNSPECIFIED``.  Values it cannot
#: map are not compacted: the router hands them back as leftovers, which go
#: to ``Record`` (with a warning when ``save_records=False``).
HK_SLEEP_VALUE_TO_STAGE = {
    "HKCategoryValueSleepAnalysisAsleepUnspecified": SleepStage.UNSPECIFIED,
    # Android / Health Connect: the SLEEPING stage, and stage-less sessions
    # from older app builds.  The legacy read path counts every value
    # starting with "HKCategoryValueSleepAnalysisAsleep" as asleep, so this
    # must map to an asleep stage to keep compact/legacy parity.
    "HKCategoryValueSleepAnalysisAsleep": SleepStage.UNSPECIFIED,
    "HKCategoryValueSleepAnalysisAsleepCore": SleepStage.CORE,
    "HKCategoryValueSleepAnalysisAsleepDeep": SleepStage.DEEP,
    "HKCategoryValueSleepAnalysisAsleepREM": SleepStage.REM,
    "HKCategoryValueSleepAnalysisAwake": SleepStage.AWAKE,
    "HKCategoryValueSleepAnalysisInBed": SleepStage.IN_BED,
    # Android / Health Connect OUT_OF_BED.  Not asleep (the legacy prefix
    # filter excludes it too); kept as awake so no stage data is lost.
    "HKCategoryValueSleepAnalysisOutOfBed": SleepStage.AWAKE,
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

#: Prefix shared by every asleep HK sleep value.  The legacy read path counts
#: any ``Record.value`` with this prefix as asleep.
SLEEP_ASLEEP_VALUE_PREFIX = "HKCategoryValueSleepAnalysisAsleep"


def sleep_stage_for_value(value: object) -> SleepStage | None:
    """Map an HK sleep value string to a :class:`SleepStage`, or ``None``.

    Known values use ``HK_SLEEP_VALUE_TO_STAGE``.  Any other value starting
    with ``SLEEP_ASLEEP_VALUE_PREFIX`` maps to ``SleepStage.UNSPECIFIED``,
    because the legacy read path counts every such value as asleep; without
    this the compact path would never see it.  Anything else (aggregates
    such as ``"100"``, unknown non-asleep values) returns ``None``.
    """
    stage = HK_SLEEP_VALUE_TO_STAGE.get(value)
    if (
        stage is None
        and isinstance(value, str)
        and value.startswith(SLEEP_ASLEEP_VALUE_PREFIX)
    ):
        return SleepStage.UNSPECIFIED
    return stage
