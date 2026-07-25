import os
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from AISData.detection import DETECTORS


class Command(BaseCommand):
    help = "Starts and monitors one independent process per detector."

    def _start_worker(self, feature_id):
        command = [
            sys.executable,
            str(settings.BASE_DIR / "manage.py"),
            "detection_worker",
            "--detector",
            feature_id,
        ]
        process = subprocess.Popen(
            command,
            cwd=str(settings.BASE_DIR),
            env=os.environ.copy(),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Started {feature_id} worker (PID {process.pid})"
            )
        )
        return process

    def handle(self, *args, **options):
        workers = {
            feature_id: self._start_worker(feature_id)
            for feature_id in DETECTORS
        }

        self.stdout.write(
            self.style.SUCCESS(
                f"Detection supervisor is monitoring {len(workers)} "
                "independent model processes."
            )
        )

        try:
            while True:
                time.sleep(2)
                for feature_id, process in list(workers.items()):
                    return_code = process.poll()
                    if return_code is None:
                        continue

                    self.stderr.write(
                        self.style.ERROR(
                            f"{feature_id} exited with code {return_code}; "
                            "restarting"
                        )
                    )
                    workers[feature_id] = self._start_worker(feature_id)
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING(
                    "Stopping all independent detection workers..."
                )
            )
            for process in workers.values():
                if process.poll() is None:
                    process.terminate()
            for process in workers.values():
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
