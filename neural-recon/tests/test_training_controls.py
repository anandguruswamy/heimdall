from __future__ import annotations

from pathlib import Path

import yaml

from nrecon.train.loop import RunMonitor, TrainConfig


def test_finite_training_loss_does_not_trigger_validation_patience():
    cfg = TrainConfig(name="test", dataset_dir="unused", early_stop_patience=1)
    monitor = RunMonitor(cfg)
    assert monitor.check_loss(1.0, 1) is None
    assert monitor.check_loss(2.0, 2) is None


def test_tuned_curriculum_configs_are_consistent():
    configs = []
    for path in sorted(Path("configs").glob("tuned-stage*.yaml")):
        configs.append(TrainConfig(**yaml.safe_load(path.read_text(encoding="utf-8"))))
    assert [cfg.max_steps for cfg in configs] == [25_000, 10_000, 10_000]
    assert all(cfg.val_scenes == 64 for cfg in configs)
    assert all(cfg.eval_every == 1_000 for cfg in configs)
    assert configs[1].replay_fraction == 0.2
    assert configs[2].replay_fraction == 0.2
