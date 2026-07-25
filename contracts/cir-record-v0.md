# CIR Record Contract v0

A CIR record represents samples captured from an actual received UWB frame.
Synthetic transport fixtures are not valid Heimdall CIR records.

Required fields:

```text
source_node_id
peer_node_id
round_id
beacon_sequence
rx_timestamp
cfo
first_path_index
sample_offset
sample_count
sample_format
samples
quality_flags
```

The first live profile uses 64 complex taps. A full diagnostic read is an
offline profile, not the default live payload.
