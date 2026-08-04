"""Physical and system constants (single source of truth).

Units: metres, nanoseconds, radians. Right-handed frame identical to
`heimdall-geometry/1` (`host-tools/radar-map/radar_map/model.py`).
"""

from __future__ import annotations

from itertools import permutations
from typing import List, Tuple

# Physical constants
C_AIR: float = 299_702_547.0  # m/s, matches the existing radar-map value

# Accumulator (system clock) rate and derived quantities
FS_HZ: float = 998.4e6  # Hz
TS_NS: float = 1e9 / FS_HZ  # ~1.0016 ns per tap
METRES_PER_TAP: float = C_AIR / FS_HZ  # ~0.3002 m per tap

# RF parameters (UWB channel 9, current Heimdall profile)
FC_HZ: float = 7.9872e9  # channel 9 center
BW_HZ: float = 499.2e6

# Hardware / contract constants
S_TAPS: int = 64  # CIR taps per link
F0_MARKER: float = 16.0  # tap of the calibration marker (see paper)
N_NODES: int = 5
L_LINKS: int = 20  # N_NODES * (N_NODES - 1) directed links
G_MAX: int = 48  # occupied superslots (3 nodes -> 48/16 = 3 per Gate H4)
W_SUPPORT: int = 16  # superslot width (slots)
OVERSAMPLE: int = 16  # fine grid step TS_NS / OVERSAMPLE for pulse kernels


def directed_links(n: int) -> List[Tuple[int, int]]:
    """Canonical directed link ordering: all (tx, rx) pairs with tx != rx,
    sorted lexicographically. Missing links are represented by a boolean
    mask elsewhere; this ordering is never reordered or compacted.
    """
    return sorted(permutations(range(n), 2))
