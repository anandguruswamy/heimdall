from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .model import VolumeResult


def export_volume(result: VolumeResult, output: Path, processing: dict[str, object], zarr: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "volume.npy", result.volume, allow_pickle=False)
    np.save(output / "confidence.npy", result.confidence, allow_pickle=False)
    metadata = dict(result.metadata)
    metadata["processing"] = processing
    metadata["files"] = {"volume": "volume.npy", "confidence": "confidence.npy"}
    if zarr:
        try:
            import zarr as zarr_module
        except ImportError as error:
            raise RuntimeError("Zarr export requested; install the optional 'zarr' package") from error
        metadata["files"]["zarr"] = "volume.zarr"
        group = zarr_module.open_group(output / "volume.zarr", mode="w")
        group.create_array("volume", data=result.volume)
        group.create_array("confidence", data=result.confidence)
        group.attrs.update(metadata)
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_volume(directory: Path) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    volume = np.load(directory / metadata["files"]["volume"], mmap_mode="r", allow_pickle=False)
    confidence = np.load(
        directory / metadata["files"]["confidence"], mmap_mode="r", allow_pickle=False
    )
    if list(volume.shape) != metadata["shape"] or confidence.shape != volume.shape:
        raise ValueError("stored volume shape does not match metadata")
    return volume, confidence, metadata
