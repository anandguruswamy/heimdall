# Fusion Events Contract v0

Fusion consumes canonical observations rather than USB frames. Initial input
events are `RangeAvailable`, `CIRAvailable`, `RoundCompleted`, `NodeMissing`,
and `NodeStatusChanged`.

Initial outputs are `RangeQualityUpdated`, `GeometryUpdated`,
`MotionEnergyUpdated`, and `HealthAlert`.

Every event includes event time, source round, participating node IDs, and a
quality/status field. Derived values must retain enough metadata to explain
which observations produced them.
