#include "beacon_schedule.h"

uint8_t heimdall_schedule_transmitter(uint32_t k, uint8_t n_nodes)
{
	return n_nodes == 0U ? 0U : (uint8_t)(k % n_nodes);
}

uint32_t heimdall_schedule_slot_index(uint32_t k, uint8_t m,
					      uint8_t slots_per_superslot)
{
	return k * (uint32_t)slots_per_superslot + m;
}

uint8_t heimdall_schedule_round_delta(uint8_t reporting_node,
					      uint8_t observed_node, uint8_t n_nodes)
{
	if (n_nodes == 0U) {
		return 0U;
	}
	return (uint8_t)((reporting_node + n_nodes - observed_node) % n_nodes);
}

uint8_t heimdall_schedule_order(uint32_t k, uint8_t reporting_node,
					uint8_t n_nodes, uint8_t ordinal)
{
	uint8_t start;
	uint8_t seen = 0U;

	if (n_nodes < 2U || ordinal >= n_nodes - 1U || reporting_node >= n_nodes) {
		return UINT8_MAX;
	}
	start = (uint8_t)(((k / n_nodes) + 1U) % n_nodes);
	for (uint8_t candidate = 0U; candidate < n_nodes; ++candidate) {
		uint8_t observed = (uint8_t)((start + candidate) % n_nodes);
		if (observed == reporting_node) {
			continue;
		}
		if (seen++ == ordinal) {
			return observed;
		}
	}
	return UINT8_MAX;
}
