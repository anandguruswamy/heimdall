# Heimdall Topology

## Initial topology

Use a deterministic seven-slot round at an initial 50 Hz target. Only the
available boards transmit; empty slots are intentional.

```text
slot 0: gateway / board 1
slot 1: peer / board 2
slot 2..6: reserved for future nodes
```

The gateway receives the same radio traffic as other nodes. Its USB connection
is an export path, not part of the UWB timing loop.

## Growth path

- N=2: link reliability, timestamps, CIR, USB export.
- N=3..4: collision and payload-budget validation.
- N=6: all-pairs geometry and room deployment.

Node identity is stable and assigned in the roster. Discovery is initially
configuration-based; autonomous discovery can be added after the radio and
payload contracts are stable.
