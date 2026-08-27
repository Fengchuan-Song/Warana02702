# AISRadar inference

This Django app uses causal AIS/Radar association adapted from the experimental
`method_JPDA`.  The former GMvA six-frame implementation and checkpoint remain
in the source tree for offline comparison, but are not the default path.

## Input

AIS and Radar tables must contain `DateTime, ID, X, Y`.  In the migrated scenes
`X` is WGS84 latitude and `Y` is longitude; association converts these values to
local east/north metres before measuring distance. Radar `GTID` is optional.
For every Radar timestamp the matcher selects each vessel's latest AIS state at
or before that timestamp, predicts it using `speed` (knots) and `course`
(degrees), and then runs JPDA/Hungarian association. Future AIS rows are never
used. The default AIS age gate is three observed AIS sampling periods; set
`AIS_RADAR_INFERENCE["max_ais_time_gap_seconds"]` for an operational feed.
`max_radar_time_gap_seconds` is also exposed for integrations that cache Radar
states; the built-in event-driven path uses the current Radar measurement, so
its radar age is always zero.

## Django API

- `GET /AISRadar/` checks the checkpoint and runtime.
- `GET /AISRadar/fused-targets/` returns the latest AIS IDs matched to Radar
  targets for the live map. The map polls this endpoint while AIS/Radar fusion
  is enabled and renders those targets in dark red.
- `POST /AISRadar/match/` accepts multipart fields `ais_file` and `radar_file`.

Each successful `match/` request publishes its latest window to
`fused-targets/`. Redis is used when available, with an in-process fallback for
single-worker development.

## Real-time replay

The migrated scene pairs in `Data/AIS-Rdar` are replayed in timestamp order
without forcing a common clock. Each sensor event publishes its own snapshot;
the replay command does not load or run JPDA. The independent fusion worker
consumes those sensor frames, runs causal JPDA using only earlier-or-equal AIS
states, and pushes the fusion state to the browser.

```powershell
python manage.py ais_radar_replay --scene 08 --interval 1 --loop
python manage.py jpda_fusion_worker
python manage.py fusion_detection_worker --detector detect-ais-off
python manage.py fusion_detection_worker --detector detect-spoofing
```

Use `--scene all` to play scenes 01 through 17. Stopping only
`ais-radar-replay` stops sensor playback without stopping JPDA or its alert
workers.

`ais_worker` and `ais_radar_replay` may run together. They use separate Redis
keys and WebSocket message types; the browser merges their current snapshots by
MMSI. If the same MMSI appears in both sources, the operational `ais_worker`
position takes precedence while the fusion state can still color it dark red.

To start Redis (when necessary), Daphne, `ais_worker`, the fusion replay, all
registered AIS detection workers, the camera stream, and overload detection
together in the background, use the project launcher:

```powershell
.\scripts\ais_radar_demo.ps1 start -Scene 08 -Interval 1
.\scripts\ais_radar_demo.ps1 status
.\scripts\ais_radar_demo.ps1 stop
```

The launcher manages separate PIDs for replay, JPDA, CloseAIS, Forgery, and each
registered detector, so `stop` does not leave child processes running. Use
`-StartDetectors:$false` or `-StartVideoDetection:$false` to disable those groups.

Runtime PID files and logs are written under `.runtime/ais-radar-demo`.

## Fusion anomaly detectors

Every completed Radar fusion frame also drives two association-based detectors:

- `CloseAIS` / `detect-ais-off`: Radar trajectories with no matched AIS target;
- `Forgery` / `detect-spoofing`: AIS trajectories with no matched Radar target.

Their latest payloads are available from `/CloseAIS/`, `/Forgery/`, and the
generic `/AISData/detection-results/<feature-id>/` endpoint. Results are also
broadcast through the existing detection WebSocket message.

By default only the latest Radar frame is inferred. Optional form fields:

- `all_windows=true` processes every Radar frame;
- `stride=N` selects the Radar-frame stride;
- `max_windows=N` limits returned windows.

Example:

```powershell
curl.exe -X POST http://127.0.0.1:8000/AISRadar/match/ `
  -F "ais_file=@AIS_08.csv" `
  -F "radar_file=@Radar_08.csv"
```

The endpoint uses Django's normal CSRF protection. Browser clients should send
the configured CSRF token.

## Standalone CLI

From the Django project root:

```powershell
python -m AISRadar.inference.cli `
  --ais-file path\to\AIS_08.csv `
  --radar-file path\to\Radar_08.csv `
  --device cuda
```

Add `--all-windows` to process a complete scene and `--output result.json` to
write JSON to disk.

Process each scene as a separate AIS/Radar pair. Concatenating unrelated scenes
can merge reused Radar IDs and corrupt GTID-based evaluation.
