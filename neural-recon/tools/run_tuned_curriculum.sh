#!/usr/bin/env bash
set -euo pipefail

init_checkpoint="$1"
out="${2:-runs/tuned-curriculum-20260806}"

python -m nrecon.train.run \
  --config configs/tuned-stage2.yaml --out "$out" \
  --init-checkpoint "$init_checkpoint"
python -m nrecon.train.run \
  --config configs/tuned-stage3.yaml --out "$out" \
  --init-checkpoint "$out/tuned-stage2/best_checkpoint.pt"
python -m nrecon.train.run \
  --config configs/tuned-stage4.yaml --out "$out" \
  --init-checkpoint "$out/tuned-stage3/best_checkpoint.pt"
