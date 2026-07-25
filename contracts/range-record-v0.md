# Range Record Contract v0

A range record contains both raw timing inputs and the derived distance. This
prevents later calibration changes from destroying the original evidence.

Required fields:

```text
initiator_node_id
responder_node_id
round_id
tx_timestamp
rx_timestamp
cfo
raw_distance_mm
corrected_distance_mm
quality_flags
```
