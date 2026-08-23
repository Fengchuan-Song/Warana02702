"""Select which AIS input may publish through the existing warning API."""

from django.core.management.base import BaseCommand, CommandError

from AISData.detection_source import activate_detection_source
from AISData.detection_queue import clear_detection_queues


class Command(BaseCommand):
    help = (
        "Activate one AIS detection source, reset transient model state, and "
        "leave the existing warning WebSocket/API contract unchanged."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--namespace",
            choices=("operational", "predict"),
            default="operational",
        )
        parser.add_argument("--simulation-id", default="")
        parser.add_argument("--force-reset", action="store_true")

    def handle(self, *args, **options):
        if options["namespace"] == "predict" and not options["simulation_id"]:
            raise CommandError("--simulation-id is required for namespace=predict")
        result = activate_detection_source(
            options["namespace"],
            options["simulation_id"],
            force=options["force_reset"],
        )
        cleared_queue_keys = 0
        if result["changed"] or options["force_reset"]:
            cleared_queue_keys = clear_detection_queues(
                options["namespace"],
                options["simulation_id"],
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Detection source active: "
                f"source={result['source_id']}, "
                f"changed={result['changed']}, "
                f"deleted_buffer_rows={result['deleted_buffer_rows']}, "
                f"cleared_queue_keys={cleared_queue_keys}"
            )
        )
