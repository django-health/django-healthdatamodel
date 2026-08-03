from django.contrib import admin

from healthdatamodel.admin import (
    ActivityDayAdmin,
    DataSourceRankingAdmin,
    RecordAdmin,
    SleepDayAdmin,
    WearableConnectionAdmin,
    WorkoutAdmin,
    WorkoutMetadataEntryAdmin,
)
from healthdatamodel.models import (
    ActivityDay,
    DataSourceRanking,
    Record,
    SleepDay,
    WearableConnection,
    Workout,
    WorkoutMetadataEntry,
)

admin.site.register(Workout, WorkoutAdmin)
admin.site.register(Record, RecordAdmin)
admin.site.register(SleepDay, SleepDayAdmin)
admin.site.register(ActivityDay, ActivityDayAdmin)
admin.site.register(DataSourceRanking, DataSourceRankingAdmin)
admin.site.register(WorkoutMetadataEntry, WorkoutMetadataEntryAdmin)
admin.site.register(WearableConnection, WearableConnectionAdmin)
