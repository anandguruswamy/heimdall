#!/usr/bin/env bash
set -euo pipefail

init_checkpoint="$1"
out="${2:-runs/full-reduced-stable-20260805}"

python -m nrecon.train.run \
  --config configs/full-reduced-stage2.yaml --out "$out" \
  --init-checkpoint "$init_checkpoint"
python -m nrecon.train.run \
  --config configs/full-reduced-stage3.yaml --out "$out" \
  --init-checkpoint "$out/full-reduced-stage2/checkpoint.pt"
python -m nrecon.train.run \
  --config configs/full-reduced-stage4.yaml --out "$out" \
  --init-checkpoint "$out/full-reduced-stage3/checkpoint.pt"
