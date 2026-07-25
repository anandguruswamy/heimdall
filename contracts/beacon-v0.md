# UWB Beacon Contract v0

Status: draft for implementation.

Each scheduled slot contains one beacon. The beacon identifies the sender and
round, carries the sender’s scheduled TX timestamp, and carries bounded reports
from prior receptions. CIR data may be fragmented across beacons.

Required semantic fields:

```text
protocol_version
sender_node_id
round_id
slot_id
beacon_sequence
scheduled_tx_timestamp
report_count
fragment_index
fragment_count
payload_crc
```

The implementation must define exact binary widths before flashing a protocol
profile. Payload budget is a hard constraint: six-peer CIR data cannot be
assumed to fit in one extended frame.
