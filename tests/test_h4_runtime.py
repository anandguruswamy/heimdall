import unittest


def slots_until_owner(k: int, node_id: int, n_nodes: int) -> int:
    for slots in range(1, n_nodes + 1):
        if ((k + slots) & 0xFFFFFFFF) % n_nodes == node_id:
            return slots
    raise AssertionError("owner not found")


class ValidationPolicy:
    def __init__(self, node_id: int) -> None:
        self.node_id = node_id
        self.identity_collision = False
        self.configuration_inhibited = False
        self.mismatches = 0
        self.matches = 0

    def receive(self, source: int, config_matches: bool) -> None:
        if not config_matches:
            self.matches = 0
            self.mismatches += 1
            self.configuration_inhibited |= self.mismatches >= 3
            return
        self.mismatches = 0
        self.matches += 1
        if self.matches >= 3:
            self.configuration_inhibited = False
        self.identity_collision |= source == self.node_id


class H4RuntimePolicyTests(unittest.TestCase):
    def test_all_n3_ownership_positions(self):
        self.assertEqual(
            [slots_until_owner(0, node, 3) for node in range(3)],
            [3, 1, 2],
        )

    def test_u32_wrap_recomputes_modulo(self):
        self.assertEqual(
            [slots_until_owner(0xFFFFFFFF, node, 3) for node in range(3)],
            [1, 2, 3],
        )

    def test_missing_intermediate_and_predecessor_use_fallback(self):
        self.assertEqual(slots_until_owner(0, 2, 3), 2)  # node 1 absent
        self.assertEqual(slots_until_owner(1, 0, 3), 2)  # node 2 absent
        self.assertEqual(slots_until_owner(3, 2, 3), 2)  # predecessor absent

    def test_duplicate_identity_is_sticky(self):
        policy = ValidationPolicy(node_id=1)
        policy.receive(source=1, config_matches=True)
        policy.receive(source=0, config_matches=True)
        self.assertTrue(policy.identity_collision)

    def test_configuration_inhibits_and_recovers_after_three_frames(self):
        policy = ValidationPolicy(node_id=0)
        for _ in range(2):
            policy.receive(source=1, config_matches=False)
        self.assertFalse(policy.configuration_inhibited)
        policy.receive(source=1, config_matches=False)
        self.assertTrue(policy.configuration_inhibited)
        for _ in range(3):
            policy.receive(source=1, config_matches=True)
        self.assertFalse(policy.configuration_inhibited)


if __name__ == "__main__":
    unittest.main()
