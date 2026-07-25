# USB CDC Contract v0

Status: draft for implementation.

```text
SYNC | VERSION | TYPE | FLAGS | LENGTH | SEQUENCE | PAYLOAD | CRC32
```

The gateway exports `HELLO`, `HEARTBEAT`, `BEACON`, `RANGE_RECORD`, `CIR_RECORD`,
`ROUND_SUMMARY`, and `ERROR` messages. The UNO Q validates length and CRC before
decoding. Invalid frames are counted and discarded; they never reach fusion.

The gateway maintains bounded queues and reports producer drops separately from
USB transport gaps.
