import os
import signal
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from AISData.detection import DETECTORS


class Command(BaseCommand):
    help = "Starts and monitors one independent process per detector."

    def add_arguments(self, parser):
        parser.add_argument(
            "--namespace",
            choices=("operational", "predict"),
            default="operational",
            help="AIS detection source namespace owned by every worker.",
        )
        parser.add_argument(
            "--simulation-id",
            default="",
            help="Required when namespace=predict.",
        )

    def _start_worker(self, feature_id, namespace, simulation_id):
        command = [
            sys.executable,
            str(settings.BASE_DIR / "manage.py"),
            "detection_worker",
            "--detector",
            feature_id,
            "--namespace",
            namespace,
            "--skip-checks",
        ]
        if simulation_id is not None:
            command.extend(["--simulation-id", simulation_id])

        process = subprocess.Popen(
            command,
            cwd=str(settings.BASE_DIR),
            env=os.environ.copy(),
        )
        source_id = (
            f"predict:{simulation_id}"
            if namespace == "predict"
            else "operational"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Started {feature_id} worker "
                f"(PID {process.pid}, source={source_id})"
            )
        )
        return process

    def _stop_workers(self, workers):
        running = [
            process
            for process in workers.values()
            if process.poll() is None
        ]
        for process in running:
            process.terminate()
        for process in running:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    @staticmethod
    def _raise_keyboard_interrupt(_signum, _frame):
        raise KeyboardInterrupt

    def handle(self, *args, **options):
        namespace = options["namespace"]
        simulation_id = options["simulation_id"].strip() or None

        if namespace == "predict" and simulation_id is None:
            raise CommandError(
                "--simulation-id is required for namespace=predict"
            )
        if namespace == "operational" and simulation_id is not None:
            raise CommandError(
                "--simulation-id is only valid for namespace=predict"
            )
        if simulation_id is not None and ":" in simulation_id:
            raise CommandError("--simulation-id cannot contain ':'")

        previous_sigterm_handler = signal.signal(
            signal.SIGTERM,
            self._raise_keyboard_interrupt,
        )
        workers = {}
        try:
            for feature_id in DETECTORS:
                workers[feature_id] = self._start_worker(
                    feature_id,
                    namespace,
                    simulation_id,
                )

            source_id = (
                f"predict:{simulation_id}"
                if namespace == "predict"
                else "operational"
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Detection supervisor is monitoring {len(workers)} "
                    f"independent model processes, source={source_id}."
                )
            )

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
                    workers[feature_id] = self._start_worker(
                        feature_id,
                        namespace,
                        simulation_id,
                    )
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING(
                    "Stopping all independent detection workers..."
                )
            )
        finally:
            self._stop_workers(workers)
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
