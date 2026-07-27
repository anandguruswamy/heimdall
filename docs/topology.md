# Heimdall Topology

## Current topology

Beacon v1 uses exactly N occupied superslots with no empty or reserved
superslots. The current N=5 profile uses two 3,500 us slots per superslot and a
35,000 us cycle, giving each node a 28.571 Hz transmit opportunity. Nodes 0-2
are hardware-qualified; nodes 3-4 are intentionally absent until their physical
boards and FICR identities are added to the roster.

```text
superslot 0: gateway / board 1, frames m=0 and m=1
superslot 1: fixed node / board 2, frames m=0 and m=1
superslot 2: fixed node / board 3, frames m=0 and m=1
superslot 3: pending board 4
superslot 4: pending board 5
```

The gateway receives the same radio traffic as other nodes. Its USB connection
is an export path, not part of the UWB timing loop.

## Qualification path

- N=3: completed link reliability, timestamps, CIR, USB export, and fallback.
- N=5 with three active nodes: completed M=2 framing and missing-node recovery.
- N=5 with all nodes: pending eight-frame gateway cycles and all 20 directed
  observations per cycle.

Node identity is stable and assigned in the roster. Discovery is initially
configuration-based; autonomous discovery can be added after the radio and
payload contracts are stable.
