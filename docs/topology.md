# Heimdall Topology

## Current topology

Beacon v1 uses exactly N occupied superslots with no empty or reserved
superslots. The current N=5 profile uses two 3,500 us slots per superslot and a
35,000 us cycle, giving each node a 28.571 Hz transmit opportunity. All five
identity-bound nodes are flashed and active.

```text
superslot 0: gateway / board 1, frames m=0 and m=1
superslot 1: fixed node / board 2, frames m=0 and m=1
superslot 2: fixed node / board 3, frames m=0 and m=1
superslot 3: fixed node / board 4, frames m=0 and m=1
superslot 4: fixed node / board 5, frames m=0 and m=1
```

The gateway receives the same radio traffic as other nodes. Its USB connection
is an export path, not part of the UWB timing loop.

## Qualification path

- N=3: completed link reliability, timestamps, CIR, USB export, and fallback.
- N=5 with three active nodes: completed M=2 framing and missing-node recovery.
- N=5 with all nodes: full 187,200 B/s gateway export and eight-frame cycles are
  hardware-qualified at 28.571 Hz. The latest 60-second placement delivered
  1,671/1,717 complete cycles; the 46 incomplete cycles each lost one radio
  frame, primarily from node 2. USB export had no post-start drops.

Node identity is stable and assigned in the roster. Discovery is initially
configuration-based; autonomous discovery can be added after the radio and
payload contracts are stable.

## Activity LEDs

D9-D12 indicate validated `m=0` reception from the four peers in ascending node
ID order, excluding the local node. D9 is green, D10-D11 are red, and D12 is
blue. Each LED toggles on reception and is forced off when its peer misses a
full expected recurrence, so a disconnected peer cannot leave an LED on. D13 is
the board power/USB indicator and is not controlled by firmware.
