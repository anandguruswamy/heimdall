from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .storage import load_volume


VIEWER_DIRECTORY = Path(__file__).with_name("viewer")


def slice_payload(
    volume: np.ndarray,
    confidence: np.ndarray,
    metadata: dict[str, object],
    plane: str,
    index: int,
) -> dict[str, object]:
    axis_sizes = {"x": volume.shape[2], "y": volume.shape[1], "z": volume.shape[0]}
    fixed_axes = {"xy": "z", "xz": "y", "yz": "x"}
    if plane not in fixed_axes:
        raise ValueError("plane must be xy, xz, or yz")
    axis = fixed_axes[plane]
    axis_size = axis_sizes[axis]
    if index < 0 or index >= axis_size:
        raise IndexError(f"{axis} index must be between 0 and {axis_size - 1}")
    if plane == "xy":
        values, weights = volume[index, :, :], confidence[index, :, :]
        labels = ["y", "x"]
    elif plane == "xz":
        values, weights = volume[:, index, :], confidence[:, index, :]
        labels = ["z", "x"]
    else:
        values, weights = volume[:, :, index], confidence[:, :, index]
        labels = ["z", "y"]
    minimum, maximum = metadata["bounds_m"][axis]
    coordinate = minimum if axis_size == 1 else minimum + index * (maximum - minimum) / (axis_size - 1)
    return {
        "schema": "heimdall-radar-slice/1",
        "plane": plane,
        "fixed_axis": axis,
        "index": index,
        "coordinate_m": coordinate,
        "array_order": labels,
        "shape": list(values.shape),
        "values": values.tolist(),
        "confidence": weights.tolist(),
    }


def point_cloud_payload(
    volume: np.ndarray,
    confidence: np.ndarray,
    metadata: dict[str, object],
    percentile: float,
    limit: int = 50_000,
) -> dict[str, object]:
    if not 0.0 <= percentile <= 100.0:
        raise ValueError("percentile must be between 0 and 100")
    if limit < 1 or limit > 100_000:
        raise ValueError("limit must be between 1 and 100000")
    valid = (confidence > 0) & np.isfinite(volume)
    valid_values = volume[valid]
    if valid_values.size == 0:
        return {
            "schema": "heimdall-radar-point-cloud/1",
            "percentile": percentile,
            "threshold": 0.0,
            "value_range": [0.0, 0.0],
            "points": [],
        }
    threshold = float(np.percentile(valid_values, percentile))
    selected = np.argwhere(valid & (volume >= threshold))
    values = volume[tuple(selected.T)]
    if len(selected) > limit:
        keep = np.argpartition(values, -limit)[-limit:]
        selected, values = selected[keep], values[keep]
    order = np.argsort(values)[::-1]
    selected, values = selected[order], values[order]
    shape = volume.shape
    bounds = metadata["bounds_m"]
    axes = {
        "x": np.linspace(*bounds["x"], shape[2]),
        "y": np.linspace(*bounds["y"], shape[1]),
        "z": np.linspace(*bounds["z"], shape[0]),
    }
    points = np.column_stack(
        (
            axes["x"][selected[:, 2]],
            axes["y"][selected[:, 1]],
            axes["z"][selected[:, 0]],
            values,
            confidence[tuple(selected.T)],
        )
    )
    return {
        "schema": "heimdall-radar-point-cloud/1",
        "percentile": percentile,
        "threshold": threshold,
        "value_range": [float(np.min(valid_values)), float(np.max(valid_values))],
        "points": points.tolist(),
    }


def make_handler(
    volume: np.ndarray,
    confidence: np.ndarray,
    metadata: dict[str, object],
    product_volumes: dict[str, np.ndarray] | None = None,
) -> type[BaseHTTPRequestHandler]:
    default_product = str(metadata.get("default_product", "motion"))
    volumes = product_volumes or {default_product: volume}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path in ("/", "/index.html", "/app.js", "/styles.css"):
                    name = "index.html" if parsed.path == "/" else parsed.path[1:]
                    self._asset(name)
                    return
                if parsed.path == "/api/v1/health":
                    self._json(
                        200,
                        {"status": "ok", "shape": list(volume.shape), "products": sorted(volumes)},
                    )
                    return
                if parsed.path == "/api/v1/metadata":
                    self._json(200, metadata)
                    return
                prefix = "/api/v1/slices/"
                if parsed.path.startswith(prefix):
                    plane = parsed.path[len(prefix):].lower()
                    query = parse_qs(parsed.query)
                    if "index" not in query:
                        raise ValueError("slice requests require an integer index query parameter")
                    product = query.get("product", [default_product])[0]
                    if product not in volumes:
                        raise ValueError(f"unknown radar product {product!r}")
                    payload = slice_payload(
                        volumes[product], confidence, metadata, plane, int(query["index"][0])
                    )
                    payload["product"] = product
                    self._json(200, payload)
                    return
                if parsed.path == "/api/v1/points":
                    query = parse_qs(parsed.query)
                    percentile = float(query.get("percentile", ["85"])[0])
                    limit = int(query.get("limit", ["50000"])[0])
                    product = query.get("product", [default_product])[0]
                    if product not in volumes:
                        raise ValueError(f"unknown radar product {product!r}")
                    payload = point_cloud_payload(
                        volumes[product], confidence, metadata, percentile, limit
                    )
                    payload["product"] = product
                    self._json(200, payload)
                    return
                self._json(404, {"error": "not found"})
            except (ValueError, IndexError) as error:
                self._json(400, {"error": str(error)})

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _asset(self, name: str) -> None:
            path = VIEWER_DIRECTORY / name
            if not path.is_file():
                self._json(404, {"error": "viewer asset not found"})
                return
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def create_server(directory: Path, host: str, port: int) -> ThreadingHTTPServer:
    volume, confidence, metadata = load_volume(directory)
    default_product = str(metadata.get("default_product", "motion"))
    volumes = {default_product: volume}
    extra_mappings = []
    for name in metadata.get("products", {}):
        if name == default_product:
            continue
        product_volume, product_confidence, _ = load_volume(directory, name)
        volumes[name] = product_volume
        extra_mappings.append(product_confidence)
    server = ThreadingHTTPServer(
        (host, port), make_handler(volume, confidence, metadata, volumes)
    )
    server.volume = volume
    server.confidence = confidence
    server.product_volumes = volumes
    server.extra_mappings = extra_mappings
    return server


def serve(directory: Path, host: str, port: int) -> None:
    server = create_server(directory, host, port)
    print(f"Serving {directory} at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
