from __future__ import annotations

from collections import Counter

import numpy as np

from .model import Geometry, GridSpec, LinkProfile, METRES_PER_TAP, QualityConfig, VolumeResult


def _scaled_cir(observation) -> np.ndarray:
    values = np.frombuffer(observation.cir_blob, dtype="<i2").reshape(-1, 2)
    raw = values[:, 0].astype(np.float64) + 1j * values[:, 1].astype(np.float64)
    correction_db = (observation.dgc_decision - 3.0) * 2.65
    gain = 10.0 ** (correction_db / 20.0)
    return raw * gain / max(1, observation.accum_count)


def _normalized_correlation(reference: np.ndarray, signal: np.ndarray) -> float:
    denominator = np.linalg.norm(reference) * np.linalg.norm(signal)
    return float(abs(np.vdot(reference, signal)) / denominator) if denominator > 0 else 0.0


def _representative_reference(rows: np.ndarray, energies: np.ndarray) -> np.ndarray:
    count = min(9, len(rows))
    candidates = np.unique(np.linspace(0, len(rows) - 1, count, dtype=int))
    candidates = np.unique(
        np.append(candidates, int(np.argmin(np.abs(energies - np.median(energies)))))
    )
    best_index = int(candidates[0])
    best_score = -1.0
    for index in candidates:
        score = float(np.median([_normalized_correlation(rows[index], row) for row in rows]))
        if score > best_score:
            best_index, best_score = int(index), score
    return rows[best_index]


def _absolute_index_anchor(
    first_paths: np.ndarray, starts: np.ndarray, energies: np.ndarray
) -> tuple[float, float]:
    weights = energies / max(float(np.median(energies)), np.finfo(np.float64).tiny)
    weights = np.clip(weights, 0.25, 4.0)

    def weighted_median(values: np.ndarray) -> float:
        order = np.argsort(values)
        ordered_weights = weights[order]
        midpoint = 0.5 * float(np.sum(ordered_weights))
        index = int(np.searchsorted(np.cumsum(ordered_weights), midpoint, side="left"))
        return float(values[order[min(index, len(order) - 1)]])

    return weighted_median(first_paths), weighted_median(starts)


def _aligned_rows(items) -> tuple[np.ndarray, np.ndarray]:
    markers = np.asarray(
        [item.fp_index_q10_6 / 64.0 - item.cir_start_offset for item in items],
        dtype=np.float64,
    )
    maximum_excess = max(item.cir_taps - marker for item, marker in zip(items, markers))
    excess_taps = np.arange(0.0, max(1.0, np.floor(maximum_excess)) + 1.0)
    rows = np.empty((len(items), len(excess_taps)), dtype=np.complex128)
    for row_index, (item, marker) in enumerate(zip(items, markers)):
        cir = _scaled_cir(item)
        sample_positions = np.arange(len(cir), dtype=np.float64) - marker
        rows[row_index] = (
            np.interp(excess_taps, sample_positions, cir.real, left=0.0, right=0.0)
            + 1j * np.interp(excess_taps, sample_positions, cir.imag, left=0.0, right=0.0)
        )
    return excess_taps, rows


def build_link_profiles(
    observations,
    geometry: Geometry,
    quality: QualityConfig = QualityConfig(),
    clutter_frames: int = 16,
) -> tuple[list[LinkProfile], dict[str, object]]:
    grouped: dict[tuple[int, int], list[object]] = {}
    rejected = Counter()
    for item in observations:
        link = (item.observed_node_id, item.reporting_node_id)
        if link[0] not in geometry.positions or link[1] not in geometry.positions:
            rejected["missing_geometry"] += 1
        elif item.obs_flags & 0x01 == 0:
            rejected["cir_invalid"] += 1
        elif item.obs_flags & 0x04 == 0:
            rejected["first_path_invalid"] += 1
        elif item.accum_count == 0:
            rejected["zero_accumulation"] += 1
        else:
            grouped.setdefault(link, []).append(item)

    profiles = []
    link_stats = []
    for (transmitter, receiver), items in sorted(grouped.items()):
        items.sort(key=lambda item: (item.observed_k, item.usb_sequence))
        decoded_rows = [_scaled_cir(item) for item in items]
        width = max(len(row) for row in decoded_rows)
        raw_rows = np.zeros((len(decoded_rows), width), dtype=np.complex128)
        for index, row in enumerate(decoded_rows):
            raw_rows[index, : len(row)] = row
        energies = np.sum(np.abs(raw_rows) ** 2, axis=1)
        median_energy = max(float(np.median(energies)), np.finfo(np.float64).tiny)
        first_paths = np.asarray([item.fp_index_q10_6 / 64.0 for item in items])
        starts = np.asarray([item.cir_start_offset for item in items])
        anchor_first_path, anchor_start = _absolute_index_anchor(first_paths, starts, energies)
        anchor_members = (
            (np.abs(first_paths - anchor_first_path) <= 1.0)
            & (np.abs(starts - anchor_start) <= 1.0)
        )
        reference_rows = raw_rows[anchor_members]
        reference_energies = energies[anchor_members]
        reference = _representative_reference(reference_rows, reference_energies)
        correlations = np.asarray([_normalized_correlation(reference, row) for row in raw_rows])
        energy_ratios = energies / median_energy
        first_path_jump = np.abs(first_paths - anchor_first_path)
        start_jump = np.abs(starts - anchor_start)
        false_path = (
            (first_path_jump > quality.max_first_path_jump_samples)
            | (start_jump > quality.max_start_offset_jump_samples)
            | (
                (correlations < quality.false_path_min_correlation)
                & (energy_ratios < quality.false_path_min_energy_ratio)
            )
        )
        low_quality = (
            (correlations < quality.minimum_correlation)
            | (energy_ratios < quality.minimum_energy_ratio)
        )
        accepted_mask = ~(false_path | low_quality)
        rejected["false_first_path"] += int(np.count_nonzero(false_path))
        rejected["low_signal_quality"] += int(np.count_nonzero(low_quality & ~false_path))
        accepted = [item for item, keep in zip(items, accepted_mask) if keep]
        if not accepted:
            continue
        if clutter_frames > 0 and len(accepted) <= clutter_frames:
            rejected["insufficient_clutter_evidence"] += len(accepted)
            continue
        excess_taps, rows = _aligned_rows(accepted)
        phase_reference = rows[0]
        for row in rows:
            cross = np.vdot(phase_reference, row)
            if abs(cross) > 0:
                row *= np.exp(-1j * np.angle(cross))
        baseline_count = max(0, clutter_frames)
        if baseline_count:
            baseline_rows = rows[:baseline_count]
            baseline = np.median(baseline_rows.real, axis=0) + 1j * np.median(
                baseline_rows.imag, axis=0
            )
            evidence_rows = rows[baseline_count:]
            magnitude = np.mean(np.abs(evidence_rows - baseline), axis=0)
        else:
            magnitude = np.mean(np.abs(rows), axis=0)
        kept_correlations = correlations[accepted_mask]
        profiles.append(
            LinkProfile(
                transmitter=transmitter,
                receiver=receiver,
                excess_taps=excess_taps,
                magnitude=magnitude.astype(np.float32),
                accepted_frames=len(accepted),
                median_correlation=float(np.median(kept_correlations)),
            )
        )
        link_stats.append(
            {
                "from": transmitter,
                "to": receiver,
                "frames": len(items),
                "accepted": len(accepted),
                "false_first_path": int(np.count_nonzero(false_path)),
                "low_signal_quality": int(np.count_nonzero(low_quality & ~false_path)),
                "median_correlation": float(np.median(kept_correlations)),
                "median_energy": median_energy,
            }
        )
    return profiles, {
        "accepted_observations": sum(profile.accepted_frames for profile in profiles),
        "rejected": dict(sorted(rejected.items())),
        "links": link_stats,
        "clutter_frames": clutter_frames,
    }


def backproject(profiles: list[LinkProfile], geometry: Geometry, grid: GridSpec) -> VolumeResult:
    x_m, y_m, z_m = grid.axes()
    zz, yy, xx = np.meshgrid(z_m, y_m, x_m, indexing="ij")
    points = np.stack((xx, yy, zz), axis=-1)
    volume = np.zeros(xx.shape, dtype=np.float64)
    confidence = np.zeros(xx.shape, dtype=np.float64)
    used_links = []
    for profile in profiles:
        transmitter = geometry.positions[profile.transmitter]
        receiver = geometry.positions[profile.receiver]
        direct_m = float(np.linalg.norm(transmitter - receiver))
        excess_m = (
            np.linalg.norm(points - transmitter, axis=-1)
            + np.linalg.norm(points - receiver, axis=-1)
            - direct_m
        )
        predicted_taps = np.maximum(0.0, excess_m / METRES_PER_TAP)
        valid = predicted_taps <= profile.excess_taps[-1]
        evidence = np.interp(
            predicted_taps.ravel(), profile.excess_taps, profile.magnitude, left=0.0, right=0.0
        ).reshape(volume.shape)
        weight = max(0.05, profile.median_correlation)
        volume += evidence * weight * valid
        confidence += weight * valid
        used_links.append(f"{profile.transmitter}>{profile.receiver}")
    np.divide(volume, confidence, out=volume, where=confidence > 0)
    return VolumeResult(
        volume=volume.astype(np.float32),
        confidence=confidence.astype(np.float32),
        x_m=x_m,
        y_m=y_m,
        z_m=z_m,
        metadata={
            "schema": "heimdall-radar-volume/1",
            "array_order": "zyx",
            "units": {"position": "m", "volume": "scaled-cir-magnitude"},
            "metres_per_cir_tap": METRES_PER_TAP,
            "shape": list(volume.shape),
            "bounds_m": {"x": [float(x_m[0]), float(x_m[-1])], "y": [float(y_m[0]), float(y_m[-1])], "z": [float(z_m[0]), float(z_m[-1])]},
            "spacing_m": grid.spacing_m,
            "geometry_revision": geometry.revision,
            "geometry_frame": geometry.frame,
            "geometry_nodes": [
                {"node_id": node_id, "position_m": position.tolist()}
                for node_id, position in sorted(geometry.positions.items())
            ],
            "directed_links": used_links,
        },
    )
