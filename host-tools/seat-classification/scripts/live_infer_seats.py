"""Live seat inference over an NDJSON stdin/stdout pipe.

Spawned once by heimdall-service when the dashboard enables live inference:
the checkpoint (weights plus the exact dB-compression and per-(link, tap)
normalization statistics it was trained with) loads a single time, then the
process loops forever reading one CIR frame per stdin line and writing one
prediction per stdout line, flushing after every line. The service terminates
the process by closing stdin (EOF). Errors go to stderr; a bad frame is
reported and skipped so the stream keeps serving.

Input line:  {"frame_id": int, "ts": float, "magnitude": [[64 floats] x 20]}
             magnitude rows follow the canonical LINK_ORDER of
             build_seat_dataset.py; for calibrated checkpoints the service
             already subtracted the frozen board references.
Output line: {"seat": str, "seat_index": int, "probs": [4 floats],
              "frame_id": ..., "ts": ...}
The first output line is a readiness handshake:
             {"ready": true, "classes": [...], "variant": str}

No matplotlib here: this must stay import-light for fast startup.
"""

import argparse
import json
import sys

import numpy as np
import torch

from train_seat_classifier import SeatCNN


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True,
                        help="path to a seat_cnn_<variant>.pt checkpoint")
    args = parser.parse_args()

    # weights_only=False: the checkpoint also stores numpy normalization stats.
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    class_names = list(ckpt["class_names"])
    model = SeatCNN(n_classes=len(class_names))
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    torch.set_num_threads(2)  # one tiny forward pass; leave cores to the DSP

    mean = np.asarray(ckpt["norm_mean"], dtype=np.float64)
    std = np.asarray(ckpt["norm_std"], dtype=np.float64)
    db_floor = float(ckpt["db_floor"])
    db_ceil = float(ckpt["db_ceil"])

    print(json.dumps({"ready": True, "classes": class_names,
                      "variant": str(ckpt.get("data_dir", "unknown"))}),
          flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
            x = np.asarray(frame["magnitude"], dtype=np.float64)
            if x.shape != mean.shape:
                raise ValueError(f"magnitude shape {x.shape} != {mean.shape}")
            # Identical preprocessing to train/evaluate: clipped dB, then
            # per-(link, tap) standardization with the training statistics.
            x = np.clip(20.0 * np.log10(x + 1e-6), db_floor, db_ceil)
            x = (x - mean) / std
            with torch.no_grad():
                logits = model(torch.from_numpy(
                    x.astype(np.float32))[None, None])
                probs = torch.softmax(logits[0], dim=0).numpy()
            seat = int(probs.argmax())
            print(json.dumps({
                "seat": class_names[seat],
                "seat_index": seat,
                "probs": [round(float(p), 4) for p in probs],
                "frame_id": frame.get("frame_id"),
                "ts": frame.get("ts"),
            }), flush=True)
        except Exception as error:  # noqa: BLE001 — keep serving later frames
            print(f"frame error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
