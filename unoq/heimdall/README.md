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
