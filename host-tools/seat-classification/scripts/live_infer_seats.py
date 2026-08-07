"""Persistent NDJSON inference worker for classifier checkpoints."""

import argparse
import json
import sys

import numpy as np
import torch

from train_seat_classifier import (SeatCNN, build_model, multilabel_result_payload,
                                   resolve_checkpoint_mode)


def _load(checkpoint):
    mode = resolve_checkpoint_mode(checkpoint)
    if checkpoint.get("model_mode") is None:
        # Legacy SeatCNN checkpoints: 4/5-way softmax, or the team-trained
        # multilabel generation marked only by multi_label=True.
        names = list(checkpoint["class_names"])
        mean = np.asarray(checkpoint["norm_mean"])
        model = SeatCNN(mean.shape[0], mean.shape[1], len(names))
        model.load_state_dict(checkpoint["state_dict"])
        return model, mode, names, []
    seat_names = list(checkpoint.get("seat_names", []))
    person_names = list(checkpoint.get("person_names", []))
    model = build_model(mode, checkpoint["architecture"], int(checkpoint["n_links"]),
                        int(checkpoint["n_taps"]), len(seat_names), len(person_names))
    model.load_state_dict(checkpoint["state_dict"])
    return model, mode, seat_names if mode != "person" else [], person_names


def _probabilities(logits):
    return torch.softmax(logits[0], dim=0).cpu().numpy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--threshold", type=float, default=None,
                        help="multilabel per-seat decision threshold "
                             "(default: checkpoint value, else 0.5)")
    args = parser.parse_args()
    if args.threshold is not None and not 0.0 < args.threshold < 1.0:
        parser.error("--threshold must be strictly between 0 and 1")
    checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    model, mode, seat_names, person_names = _load(checkpoint)
    threshold = (args.threshold if args.threshold is not None
                 else float(checkpoint.get("threshold", 0.5)))
    model.eval()
    torch.set_num_threads(2)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    mean = np.asarray(checkpoint["norm_mean"], dtype=np.float64)
    std = np.asarray(checkpoint["norm_std"], dtype=np.float64)
    floor = float(checkpoint["db_floor"])
    ceil = float(checkpoint["db_ceil"])
    variant = str(checkpoint.get("variant", checkpoint.get("data_dir", "unknown")))
    jit_optimized = False
    try:
        example = torch.zeros((1, 1, *mean.shape), dtype=torch.float32)
        with torch.inference_mode():
            model = torch.jit.optimize_for_inference(
                torch.jit.freeze(torch.jit.trace(model, example, strict=False).eval()))
            model(example)  # Warm kernels before the readiness handshake.
        jit_optimized = True
    except (RuntimeError, TypeError, ValueError):
        # Legacy/runtime combinations that cannot trace still use eager mode.
        model.eval()
    if mode == "multilabel":
        # seat_classes/classes stay empty so no five-class consumer misfires
        # on the four independent bit names.
        readiness = {
            "ready": True, "mode": mode, "seat_bits": seat_names,
            "seat_classes": [], "person_classes": [], "classes": [],
            "threshold": threshold, "variant": variant, "jit_optimized": jit_optimized,
            "features": {"shape": list(mean.shape), "input": "magnitude",
                         "link_mode": str(checkpoint.get("link_mode", "directed")),
                         "taps_left": int(checkpoint.get("taps_left", 0)),
                         "taps_right": int(checkpoint.get("taps_right", mean.shape[1] - 1)),
                         "crop": str(checkpoint.get("crop", "full"))},
        }
    else:
        readiness = {
            "ready": True, "mode": mode, "seat_classes": seat_names,
            "person_classes": person_names, "classes": seat_names or person_names,
            "variant": variant, "jit_optimized": jit_optimized,
            "features": {"shape": list(mean.shape), "input": "cropped_magnitude",
                         "link_mode": str(checkpoint.get("link_mode", "unknown")),
                         "taps_left": int(checkpoint.get("taps_left", 0)),
                         "taps_right": int(checkpoint.get("taps_right", mean.shape[1] - 1))},
        }
    print(json.dumps(readiness, separators=(",", ":")), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
            x = np.asarray(frame["magnitude"], dtype=np.float64)
            if x.shape != mean.shape:
                raise ValueError(f"magnitude shape {x.shape} != {mean.shape}")
            x = np.clip(20.0 * np.log10(x + 1e-6), floor, ceil)
            x = (x - mean) / std
            with torch.inference_mode():
                output = model(torch.from_numpy(x.astype(np.float32))[None, None])
            if isinstance(output, torch.Tensor):
                output = {"seat": output}
            if mode == "multilabel":
                probabilities = torch.sigmoid(output["seat"][0]).cpu().numpy()
                payload = multilabel_result_payload(probabilities, seat_names, threshold,
                                                    frame.get("frame_id"), frame.get("ts"))
                print(json.dumps(payload, separators=(",", ":")), flush=True)
                continue
            result = {"frame_id": frame.get("frame_id"), "ts": frame.get("ts")}
            seat_index = None
            if "seat" in output:
                probabilities = _probabilities(output["seat"])
                seat_index = int(probabilities.argmax())
                values = [round(float(value), 4) for value in probabilities]
                result.update({"raw_seat": seat_names[seat_index],
                               "raw_seat_index": seat_index, "raw_seat_probs": values,
                               "seat": seat_names[seat_index], "seat_index": seat_index,
                               "probs": values})
            if "person" in output:
                probabilities = _probabilities(output["person"])
                person_index = int(probabilities.argmax())
                person = person_names[person_index]
                if seat_index is not None and seat_names[seat_index] == "Empty":
                    person, person_index = "n/a", -1
                result.update({"raw_person": person, "raw_person_index": person_index,
                               "raw_person_probs": [round(float(value), 4)
                                                    for value in probabilities]})
            print(json.dumps(result, separators=(",", ":")), flush=True)
        except Exception as error:  # Keep serving subsequent frames.
            print(f"frame error: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
