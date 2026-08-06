#!/usr/bin/env bash
set -euo pipefail

out="${1:-runs/full-reduced-20260805}"

python -m nrecon.train.run \
  --config configs/full-reduced-stage1.yaml --out "$out"
python -m nrecon.train.run \
  --config configs/full-reduced-stage2.yaml --out "$out" \
  --init-checkpoint "$out/full-reduced-stage1/checkpoint.pt"
python -m nrecon.train.run \
  --config configs/full-reduced-stage3.yaml --out "$out" \
  --init-checkpoint "$out/full-reduced-stage2/checkpoint.pt"
python -m nrecon.train.run \
  --config configs/full-reduced-stage4.yaml --out "$out" \
  --init-checkpoint "$out/full-reduced-stage3/checkpoint.pt"
