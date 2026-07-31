from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import numpy as np


GEOMETRY_SCHEMA = "heimdall-geometry/1"
METRES_PER_TAP = 299_702_547.0 / 998_400_000.0


@dataclass(frozen=True)
class Geometry:
    positions: dict[int, np.ndarray]
    frame: dict[str, object] = field(default_factory=dict)
    revision: str | None = None
    provenance: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Geometry":
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") != GEOMETRY_SCHEMA:
            raise ValueError(f"geometry schema must be {GEOMETRY_SCHEMA!r}")
        if document.get("units") != "m":
            raise ValueError("geometry units must be metres ('m')")
        positions: dict[int, np.ndarray] = {}
        for node in document.get("nodes", []):
            node_id = node.get("node_id")
            position = np.asarray(node.get("position_m"), dtype=np.float64)
            if not isinstance(node_id, int) or node_id < 0:
                raise ValueError("each geometry node must have a non-negative integer node_id")
            if node_id in positions:
                raise ValueError(f"duplicate geometry node_id {node_id}")
            if position.shape != (3,) or not np.all(np.isfinite(position)):
                raise ValueError(f"node {node_id} position_m must contain three finite values")
            positions[node_id] = position
        if len(positions) < 2:
            raise ValueError("geometry must contain at least two nodes")
        return cls(
            positions=positions,
            frame=dict(document.get("frame", {})),
            revision=document.get("revision"),
            provenance=dict(document.get("provenance", {})),
        )


@dataclass(frozen=True)
class GridSpec:
    minimum_m: tuple[float, float, float]
    maximum_m: tuple[float, float, float]
    spacing_m: float

    def axes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not np.isfinite(self.spacing_m) or self.spacing_m <= 0:
            raise ValueError("grid spacing must be finite and positive")
        axes = []
        for minimum, maximum in zip(self.minimum_m, self.maximum_m):
            if not np.isfinite(minimum) or not np.isfinite(maximum) or maximum < minimum:
                raise ValueError("grid bounds must be finite and ordered")
            count = int(np.floor((maximum - minimum) / self.spacing_m + 1e-9)) + 1
            axes.append(minimum + np.arange(count, dtype=np.float64) * self.spacing_m)
        return axes[0], axes[1], axes[2]


@dataclass(frozen=True)
class QualityConfig:
    max_first_path_jump_samples: float = 8.0
    max_start_offset_jump_samples: float = 8.0
    false_path_min_correlation: float = 0.65
    false_path_min_energy_ratio: float = 0.20
    minimum_correlation: float = 0.25
    minimum_energy_ratio: float = 0.10


@dataclass(frozen=True)
class LinkProfile:
    transmitter: int
    receiver: int
    excess_taps: np.ndarray
    magnitude: np.ndarray
    accepted_frames: int
    median_correlation: float
    static_magnitude: np.ndarray | None = None


@dataclass(frozen=True)
class VolumeResult:
    volume: np.ndarray
    confidence: np.ndarray
    x_m: np.ndarray
    y_m: np.ndarray
    z_m: np.ndarray
    metadata: dict[str, object]
