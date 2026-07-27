# UNO Q Heimdall Runtime

The runtime implementation will be added behind these seams:

```text
cdc_gateway.py  ->  protocol.py  ->  canonical observations
capture replay  ------------------^             |
                                                v
                         fusion.py + storage.py + dashboard
```

The first implementation should read framed CDC records, validate CRC and
sequence continuity, append raw and canonical records, and replay the same
records through a testable fusion interface.

Radio-specific details stay in the decoder and contracts; fusion receives
normalized observations.

The implemented first adapter provides:

- `protocol.py`: incremental framing, CRC and sequence validation, plus record
  and CIR subreport decoding.
- `cdc_gateway.py`: raw Linux CDC capture with optional `--seconds` duration.
- `replay.py`: byte-exact capture replay through the same stream parser.
- `inspect_capture.py`: validate and summarize a raw capture after waiting for
  the first periodic `HELLO`.

Run a finite capture on the UNO Q with:

```bash
python3 -m heimdall.cdc_gateway /dev/ttyACM2 capture.husb --seconds 10
```

Use the stable `/dev/serial/by-id/` link when available instead of assuming the
example ACM number.
