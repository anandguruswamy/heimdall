# UNO Q Heimdall Runtime

The runtime is implemented behind these seams:

```text
ingest_cdc.py -> archive.py -> protocol.py -> canonical.py -> storage.py
archive replay -------------------^                    |
                                                        v
                                               future fusion/dashboard
```

The ingest service archives bytes before parsing, validates outer and
type-specific integrity, records reconnect/configuration epochs in SQLite, and
stores local and relayed CIR reports in one canonical observation schema.

Radio-specific details stay in the decoder and contracts; fusion receives
normalized observations.

The implemented first adapter provides:

- `protocol.py`: incremental framing, CRC and sequence validation, plus record
  and CIR subreport decoding.
- `cdc_gateway.py`: raw Linux CDC capture with optional `--seconds` duration.
- `replay.py`: byte-exact capture replay through the same stream parser.
- `inspect_capture.py`: validate and summarize a raw capture after waiting for
  the first periodic `HELLO`.
- `beacon.py` and `canonical.py`: validate and reassemble arbitrary-`M` beacon
  reports, then normalize local and relayed observations.
- `archive.py` and `storage.py`: rotating byte-exact raw segments and
  transactional SQLite persistence.
- `ingest_cdc.py`: reconnecting Linux CDC service; a reconnect starts a new
  connection epoch without affecting radio operation.
- `replay_ingest.py`: feed ordered archive segments through the production
  parser and canonical path into a fresh database.

Run a finite capture on the UNO Q with:

```bash
python3 -m heimdall.cdc_gateway /dev/ttyACM2 capture.husb --seconds 10
```

Use the stable `/dev/serial/by-id/` link when available instead of assuming the
example ACM number.

Start persistent ingestion from the `unoq/` directory with a JSON file that
contains the run metadata required by `docs/operations.md`:

```bash
python3 -m heimdall.ingest_cdc \
  /dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00 \
  data/heimdall.sqlite3 data/raw --metadata run-metadata.json
```

The default segment size is 64 MiB. Use `--rotate-bytes` with a smaller value
for the Gate H3 rotation test.

Each `data/raw/connection-NNNNNN/` directory is independently replayable:

```bash
python3 -m heimdall.replay_ingest \
  data/raw/connection-000001 data/replay.sqlite3 \
  --metadata run-metadata.json --chunk-size 65536
```

Use a new database for replay. Comparing `observation_fingerprints()` between
the live and replay run verifies canonical observation equivalence independent
of SQLite row identifiers.
