from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .storage import load_volume


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


def serve(directory: Path, host: str, port: int) -> None:
    volume, confidence, metadata = load_volume(directory)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/v1/health":
                    self._json(200, {"status": "ok", "shape": list(volume.shape)})
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
                    payload = slice_payload(volume, confidence, metadata, plane, int(query["index"][0]))
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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving {directory} at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
