# AISRadar inference

This Django app contains the migrated GMvA AIS/Radar inference pipeline and the
canonical seed-42 checkpoint. Training code is intentionally not included.

## Input

AIS and Radar tables must contain `DateTime, ID, X, Y`. Radar `GTID` is optional;
when present, precision/recall are included in the response. At least six common
timestamps are required, and a trajectory must have at least three valid points
inside a six-frame window.

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

The migrated scene pairs in `Data/AIS-Rdar` can be replayed by common timestamp.
Each frame publishes both AIS and Radar snapshots over the existing AIS
WebSocket group. After six frames, the command runs rolling inference and pushes
the fusion state immediately to the browser.

```powershell
python manage.py ais_radar_replay --scene 08 --interval 1 --loop
```

Use `--scene all` to play scenes 01 through 17, `--device cpu` to force CPU,
or `--no-inference` to test sensor delivery without loading the model.

`ais_worker` and `ais_radar_replay` may run together. They use separate Redis
keys and WebSocket message types; the browser merges their current snapshots by
MMSI. If the same MMSI appears in both sources, the operational `ais_worker`
position takes precedence while the fusion state can still color it dark red.

To start Redis (when necessary), Daphne, `ais_worker`, and the fusion replay
together in the background, use the project launcher:

```powershell
.\scripts\ais_radar_demo.ps1 start -Scene 08 -Interval 1
.\scripts\ais_radar_demo.ps1 status
.\scripts\ais_radar_demo.ps1 stop
```

Runtime PID files and logs are written under `.runtime/ais-radar-demo`.

By default only the latest six-frame window is inferred. Optional form fields:

- `all_windows=true` processes the full file pair;
- `stride=N` selects the sliding-window stride;
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
