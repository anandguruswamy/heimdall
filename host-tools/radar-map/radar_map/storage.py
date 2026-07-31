from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .model import VolumeResult


def export_volume(
    result: VolumeResult,
    output: Path,
    processing: dict[str, object],
    zarr: bool = False,
    additional_products: dict[str, VolumeResult] | None = None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "volume.npy", result.volume, allow_pickle=False)
    np.save(output / "confidence.npy", result.confidence, allow_pickle=False)
    metadata = dict(result.metadata)
    metadata["processing"] = processing
    metadata["files"] = {"volume": "volume.npy", "confidence": "confidence.npy"}
    metadata["default_product"] = result.metadata.get("product", "motion")
    metadata["products"] = {
        metadata["default_product"]: {"volume": "volume.npy", "confidence": "confidence.npy"}
    }
    for name, product in (additional_products or {}).items():
        filename = f"{name}-volume.npy"
        np.save(output / filename, product.volume, allow_pickle=False)
        metadata["products"][name] = {"volume": filename, "confidence": "confidence.npy"}
    if zarr:
        try:
            import zarr as zarr_module
        except ImportError as error:
            raise RuntimeError("Zarr export requested; install the optional 'zarr' package") from error
        metadata["files"]["zarr"] = "volume.zarr"
        group = zarr_module.open_group(output / "volume.zarr", mode="w")
        group.create_array("volume", data=result.volume)
        group.create_array("confidence", data=result.confidence)
        for name, product in (additional_products or {}).items():
            group.create_array(f"{name}_volume", data=product.volume)
        group.attrs.update(metadata)
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_volume(
    directory: Path, product: str | None = None
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if product is not None:
        products = metadata.get("products", {})
        if product not in products:
            raise ValueError(f"stored volume has no {product!r} product")
        files = products[product]
    else:
        files = metadata["files"]
    volume = np.load(directory / files["volume"], mmap_mode="r", allow_pickle=False)
    confidence = np.load(
        directory / files["confidence"], mmap_mode="r", allow_pickle=False
    )
    if list(volume.shape) != metadata["shape"] or confidence.shape != volume.shape:
        raise ValueError("stored volume shape does not match metadata")
    return volume, confidence, metadata
