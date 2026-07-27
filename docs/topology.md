# Heimdall Topology

## Initial topology

Beacon v1 uses exactly N occupied superslots with no empty or reserved
superslots. The validated N=2 deployment uses 10,000 us superslots and a
20,000 us cycle, giving each node a 50 Hz transmit opportunity.

```text
slot 0: gateway / board 1
slot 1: peer / board 2
```

The gateway receives the same radio traffic as other nodes. Its USB connection
is an export path, not part of the UWB timing loop.

## Growth path

- N=2: link reliability, timestamps, CIR, USB export.
- N=3: add one occupied superslot for node 2; at the initial 10,000 us profile,
  the cycle becomes 30,000 us and each node transmits at 33.333 Hz.
- N=4: collision and payload-budget validation after N=3 passes.
- N=6: all-pairs geometry and room deployment.

Node identity is stable and assigned in the roster. Discovery is initially
configuration-based; autonomous discovery can be added after the radio and
payload contracts are stable.
