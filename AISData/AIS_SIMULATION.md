# Local AIS receiver simulation

中文完整参数说明与“多个 CSV + 无限循环播放”示例见
[simulate_ais_realtime 使用说明](SIMULATE_AIS_REALTIME_USAGE_ZH.md)。

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

The frontend always connects to `/ws/ais/`. The launch profile selects whether
that unchanged route serves operational or Predict AIS state.

The dedicated PowerShell launcher starts or reuses Redis and Daphne, activates
an isolated Predict detection source, starts its detector workers, and then
starts the simulator. It does not start `ais_worker`, video workers or the
older AIS/Radar replay:

```powershell
.\scripts\ais_predict_demo.ps1 start
```

The launcher uses port `8000`, matching the existing demo. The operational and
Predict profiles are mutually exclusive, so stop one profile before starting
the other. Open `http://127.0.0.1:8000/` after startup; no query parameter is
required.

Use `status` and `stop` to inspect or stop only processes managed by this
launcher. Redis and an externally managed Daphne process are never stopped.
Stopping the launcher restores the operational source for the unchanged
`/ws/violations/` warning interface. Pass `-StartDetectors:$false` for an AIS
transport-only replay.

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
