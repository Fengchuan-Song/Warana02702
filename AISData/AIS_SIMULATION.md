# Local AIS receiver simulation

`simulate_ais_realtime` converts historical decoded AIS CSV rows into a
delivery-ordered local stream. It uses source timestamps as event time and
sets `received_at` when a batch reaches the application.

## Safe Predict replay

The default namespace is `predict`, which uses independent cache keys and the
`ais_predict_updates` Channels group. It does not enqueue violation detectors.

```powershell
python manage.py simulate_ais_realtime `
  --file "H:\全球数据\cleaned_v3\retime\2020-12-27_cleaned_retime.csv" `
  --mode received `
  --source-timezone Asia/Shanghai `
  --speed-factor 10 `
  --max-ships 200 `
  --duration-minutes 60
```

Open `/?ais_source=predict` to use `/ws/ais-predict/` instead of the operational
AIS WebSocket.

The dedicated PowerShell launcher starts or reuses Redis and Daphne, then
starts only the isolated Predict simulator. It does not start `ais_worker`,
detectors, video workers or the older AIS/Radar replay:

```powershell
.\scripts\ais_predict_demo.ps1 start
```

The launcher defaults to port `8001` so it can run beside the existing demo on
port `8000` without restarting it. Open
`http://127.0.0.1:8001/?ais_source=predict` after startup.

Use `status` and `stop` to inspect or stop only processes managed by this
launcher. Redis and an externally managed Daphne process are never stopped.

Modes:

- `observed`: preserve source rows and source intervals.
- `broadcast`: generate nominal Class A/B position reports between usable
  source anchors.
- `received`: apply packet loss, latency, duplication, reordering and receiver
  outages to the nominal broadcast stream.

Use `--dry-run --no-wait` to validate a source without cache or WebSocket
writes. `--max-events` bounds a smoke or load test.

## Operational namespace guard

Writing to operational AIS state is blocked unless explicitly authorized:

```powershell
python manage.py simulate_ais_realtime ... `
  --namespace operational `
  --allow-operational-write
```

`--enqueue-detections` is accepted only in the operational namespace. Use it
only with an isolated local database, Redis database and detector workers.
