from django.db import migrations
from django.db.models import Max, Min


def backfill_detection_times(apps, schema_editor):
    Event = apps.get_model("AISData", "ViolationEventRecord")
    Trajectory = apps.get_model("AISData", "ViolationAISTrajectoryPoint")
    pending_updates = []
    for event in Event.objects.all().iterator(chunk_size=500):
        bounds = Trajectory.objects.filter(event_id=event.id).aggregate(
            first=Min("observed_at"),
            last=Max("observed_at"),
        )
        first = bounds["first"] or event.event_time
        last = bounds["last"] or event.event_time
        if first is None or last is None:
            continue
        event.first_detected_at = first
        event.last_detected_at = last
        pending_updates.append(event)
        if len(pending_updates) >= 500:
            Event.objects.bulk_update(
                pending_updates,
                ("first_detected_at", "last_detected_at"),
            )
            pending_updates.clear()
    if pending_updates:
        Event.objects.bulk_update(
            pending_updates,
            ("first_detected_at", "last_detected_at"),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("AISData", "0001_violationeventrecord"),
    ]

    operations = [
        migrations.RunPython(
            backfill_detection_times,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
