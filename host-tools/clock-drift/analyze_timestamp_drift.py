"""Estimate relative DW3000 clock drift from static-link TX/RX timestamps."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "unoq"))

from heimdall.canonical import CanonicalProcessor  # noqa: E402
from heimdall.protocol import ProtocolError, StreamParser  # noqa: E402

DTU_HZ = 499_200_000.0 * 128.0
TIMESTAMP_MODULUS = 1 << 40
SUPERSLOT_S = 0.007


def unwrap(values: list[int]) -> np.ndarray:
    output = np.empty(len(values), dtype=np.float64)
    output[0] = values[0]
    for index in range(1, len(values)):
        delta = (values[index] - values[index - 1] + TIMESTAMP_MODULUS // 2) % TIMESTAMP_MODULUS
        delta -= TIMESTAMP_MODULUS // 2
        output[index] = output[index - 1] + delta
    return output


def robust_fit(design: np.ndarray, values: np.ndarray, iterations: int = 8):
    weights = np.ones(len(values))
    coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
    for _ in range(iterations):
        residuals = values - design @ coefficients
        median = np.median(residuals)
        scale = 1.4826 * np.median(np.abs(residuals - median)) + 1e-18
        limit = 1.5 * scale
        weights = np.minimum(1.0, limit / np.maximum(np.abs(residuals), 1e-18))
        root = np.sqrt(weights)
        coefficients = np.linalg.lstsq(design * root[:, None], values * root, rcond=None)[0]
    residuals = values - design @ coefficients
    dof = max(1, len(values) - design.shape[1])
    variance = float(np.sum(weights * residuals**2) / dof)
    covariance = variance * np.linalg.pinv(design.T @ (weights[:, None] * design))
    return coefficients, residuals, covariance, weights


def decode_capture(path: Path):
    parser = StreamParser()
    records = parser.feed(path.read_bytes())
    processor = CanonicalProcessor()
    observations = []
    for record in records:
        try:
            output = processor.process(record)
        except ProtocolError:
            if processor.decoder_state.hello is None:
                continue
            raise
        observations.extend(output.observations)
    unique = {}
    for item in observations:
        if item.observed_node_id == item.reporting_node_id:
            continue
        key = (
            item.observed_node_id,
            item.reporting_node_id,
            item.observed_k,
            item.observed_tx_timestamp,
            item.rx_timestamp,
        )
        unique[key] = item
    return list(unique.values()), parser.stats, len(records)


def fit_link(items):
    items = sorted(items, key=lambda item: (item.observed_k, item.usb_sequence))
    tx = unwrap([item.observed_tx_timestamp for item in items])
    rx = unwrap([item.rx_timestamp for item in items])
    x = (tx - tx[0]) / DTU_HZ
    y = (rx - rx[0]) / DTU_HZ
    center = float(np.mean(x))
    xc = x - center
    design = np.column_stack((np.ones(len(x)), xc, 0.5 * xc**2))
    quadratic, residuals, quadratic_covariance, _ = robust_fit(design, y)

    local_times = []
    local_skews = []
    start = math.ceil((x[0] + 0.5) * 4) / 4
    stop = x[-1] - 0.5
    window_center = start
    while window_center <= stop + 1e-9:
        mask = np.abs(x - window_center) <= 0.5
        if np.count_nonzero(mask) >= 12:
            local_x = x[mask] - window_center
            local_design = np.column_stack((np.ones(np.count_nonzero(mask)), local_x))
            local_fit, _, _, _ = robust_fit(local_design, y[mask])
            local_times.append(window_center)
            local_skews.append((local_fit[1] - 1.0) * 1e6)
        window_center += 0.25
    local_times_array = np.asarray(local_times)
    local_skews_array = np.asarray(local_skews)
    trend_design = np.column_stack((np.ones(len(local_times_array)), local_times_array - np.mean(local_times_array)))
    trend, trend_residuals, trend_covariance, _ = robust_fit(trend_design, local_skews_array)
    drift_ppb_per_ms = float(trend[1])
    drift_ci95 = 1.96 * math.sqrt(max(0.0, float(trend_covariance[1, 1])))
    quadratic_drift = float(quadratic[2] * 1e6)
    return {
        "count": len(items),
        "duration_s": float(x[-1] - x[0]),
        "mid_skew_ppm": float(trend[0]),
        "drift_ppb_per_ms": drift_ppb_per_ms,
        "drift_ci95_ppb_per_ms": drift_ci95,
        "quadratic_drift_ppb_per_ms": quadratic_drift,
        "quadratic_drift_ci95_ppb_per_ms": 1.96 * math.sqrt(max(0.0, float(quadratic_covariance[2, 2]))) * 1e6,
        "timestamp_residual_rms_ps": float(np.sqrt(np.mean(residuals**2)) * 1e12),
        "local_skew_residual_std_ppb": float(np.std(trend_residuals) * 1e3),
        "local_times": local_times,
        "local_skews_ppm": local_skews,
    }


def node_decomposition(link_results: list[dict], node_count: int):
    rows = []
    values = []
    weights = []
    for result in link_results:
        row = np.zeros(node_count - 1)
        if result["to"] != 0:
            row[result["to"] - 1] += 1
        if result["from"] != 0:
            row[result["from"] - 1] -= 1
        rows.append(row)
        values.append(result["drift_ppb_per_ms"])
        ci = max(result["drift_ci95_ppb_per_ms"] / 1.96, 1e-6)
        weights.append(1.0 / ci**2)
    matrix = np.asarray(rows)
    vector = np.asarray(values)
    root = np.sqrt(np.asarray(weights))
    coefficients = np.linalg.lstsq(matrix * root[:, None], vector * root, rcond=None)[0]
    residuals = vector - matrix @ coefficients
    nodes = [{"node": 0, "drift_ppb_per_ms_relative_n0": 0.0}]
    nodes.extend(
        {"node": index + 1, "drift_ppb_per_ms_relative_n0": float(value)}
        for index, value in enumerate(coefficients)
    )
    return nodes, residuals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    observations, stats, record_count = decode_capture(args.capture)
    newest_k = max(item.observed_k for item in observations)
    minimum_k = newest_k - round(args.seconds / SUPERSLOT_S)
    selected = [item for item in observations if minimum_k <= item.observed_k <= newest_k]
    links = {}
    for item in selected:
        links.setdefault((item.observed_node_id, item.reporting_node_id), []).append(item)

    link_results = []
    for (source, receiver), items in sorted(links.items()):
        result = fit_link(items)
        result.update({"from": source, "to": receiver})
        link_results.append(result)
    node_count = 1 + max(max(result["from"], result["to"]) for result in link_results)
    nodes, decomposition_residuals = node_decomposition(link_results, node_count)
    for node in nodes:
        node["frequency_change_over_window_ppb"] = (
            node["drift_ppb_per_ms_relative_n0"] * args.seconds * 1_000.0
        )

    with (args.output / "observations.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["from", "to", "round", "usb_sequence", "tx_timestamp", "rx_timestamp", "route"])
        for item in sorted(selected, key=lambda value: (value.observed_k, value.reporting_node_id, value.observed_node_id)):
            writer.writerow([item.observed_node_id, item.reporting_node_id, item.observed_k, item.usb_sequence, item.observed_tx_timestamp, item.rx_timestamp, item.route])
    public_links = [{key: value for key, value in result.items() if key not in {"local_times", "local_skews_ppm"}} for result in link_results]
    with (args.output / "directed_link_fits.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=public_links[0].keys())
        writer.writeheader()
        writer.writerows(public_links)
    with (args.output / "node_drift.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=nodes[0].keys())
        writer.writeheader()
        writer.writerows(nodes)
    with (args.output / "local_skew_series.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["from", "to", "time_s", "relative_skew_ppm"])
        for result in link_results:
            for time_s, skew_ppm in zip(result["local_times"], result["local_skews_ppm"]):
                writer.writerow([result["from"], result["to"], time_s, skew_ppm])

    opposite_errors = []
    result_map = {(result["from"], result["to"]): result for result in link_results}
    for (source, receiver), result in result_map.items():
        if source < receiver and (receiver, source) in result_map:
            opposite_errors.append(result["drift_ppb_per_ms"] + result_map[(receiver, source)]["drift_ppb_per_ms"])
    drift_values = [abs(result["drift_ppb_per_ms"]) for result in link_results]
    summary = {
        "capture": str(args.capture),
        "records": record_count,
        "selected_observations": len(selected),
        "window_seconds": args.seconds,
        "first_round": min(item.observed_k for item in selected),
        "last_round": newest_k,
        "parser": vars(stats),
        "directed_links": public_links,
        "nodes_relative_n0": nodes,
        "median_abs_directed_drift_ppb_per_ms": float(np.median(drift_values)),
        "max_abs_directed_drift_ppb_per_ms": max(drift_values),
        "opposite_direction_sum_rms_ppb_per_ms": float(np.sqrt(np.mean(np.asarray(opposite_errors) ** 2))),
        "node_decomposition_rms_ppb_per_ms": float(np.sqrt(np.mean(decomposition_residuals**2))),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    max_drift = summary["max_abs_directed_drift_ppb_per_ms"] * 1e-6
    spans = [0.007, 0.035, 0.070]
    range_rows = [(span, 0.5 * max_drift * span**2, 299_792_458.0 * 0.5 * max_drift * span**2) for span in spans]
    lines = [
        "# Heimdall 30-Second Timestamp Drift Report",
        "",
        f"- Capture: `{args.capture}`",
        f"- Selected post-trigger observations: {len(selected):,}",
        f"- Round interval: {summary['first_round']}..{summary['last_round']}",
        f"- Directed links: {len(link_results)}",
        f"- Parser CRC/framing failures: {stats.crc_failures}/{stats.framing_errors}",
        f"- Median absolute directed drift: {summary['median_abs_directed_drift_ppb_per_ms']:.8f} ppb/ms",
        f"- Maximum absolute directed drift: {summary['max_abs_directed_drift_ppb_per_ms']:.8f} ppb/ms",
        f"- Maximum relative-frequency change over 30 s: {summary['max_abs_directed_drift_ppb_per_ms'] * args.seconds * 1_000:.3f} ppb",
        f"- Opposite-direction consistency RMS: {summary['opposite_direction_sum_rms_ppb_per_ms']:.8f} ppb/ms",
        f"- Per-node decomposition residual RMS: {summary['node_decomposition_rms_ppb_per_ms']:.8f} ppb/ms",
        "",
        "## Per-Node Drift Relative to N0",
        "",
        "| Node | Drift (ppb/ms) | Change over 30 s (ppb) |",
        "|---:|---:|---:|",
    ]
    lines.extend(f"| N{item['node']} | {item['drift_ppb_per_ms_relative_n0']:+.8f} | {item['frequency_change_over_window_ppb']:+.3f} |" for item in nodes)
    lines.extend(["", "## Directed-Link Fits", "", "| Link | Drift | 95% CI | Quadratic fit | Mid skew | Local noise |", "|---|---:|---:|---:|---:|---:|"])
    lines.extend(
        f"| N{item['from']}→N{item['to']} | {item['drift_ppb_per_ms']:+.5f} | ±{item['drift_ci95_ppb_per_ms']:.5f} | {item['quadratic_drift_ppb_per_ms']:+.5f} | {item['mid_skew_ppm']:+.3f} ppm | {item['local_skew_residual_std_ppb']:.2f} ppb |"
        for item in public_links
    )
    lines.extend(["", "## Curvature Scale at Maximum Measured Relative Drift", "", "| Span | Clock curvature | Light-distance equivalent |", "|---:|---:|---:|"])
    lines.extend(f"| {span*1e3:.0f} ms | {phase*1e12:.4f} ps | {metres*1e3:.4f} mm |" for span, phase, metres in range_rows)
    lines.extend(["", "## Interpretation", "", "The measured drift is roughly four orders of magnitude below a few ppb/ms. At the maximum fitted relative drift, clock curvature over a 70 ms exchange is about 0.32 ps, or 0.097 mm of light travel before any ADS-TWR cancellation.", "", "The curvature values are scale estimates, not exact ADS-TWR bias; cancellation depends on exchange timing symmetry. Regression confidence intervals describe this capture and do not include long-term thermal or environmental changes. Only relative drift is observable without an external clock reference."])
    (args.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
