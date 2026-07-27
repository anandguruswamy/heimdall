#pragma once

#include <stdint.h>

uint8_t heimdall_schedule_transmitter(uint32_t k, uint8_t n_nodes);
uint8_t heimdall_schedule_slots_until_owner(uint32_t k, uint8_t node_id,
					    uint8_t n_nodes);
uint8_t heimdall_schedule_frame_slots_until_owner(uint32_t k, uint8_t m,
						  uint8_t node_id, uint8_t n_nodes,
						  uint8_t slots_per_superslot);
uint32_t heimdall_schedule_slot_index(uint32_t k, uint8_t m,
					      uint8_t slots_per_superslot);
uint8_t heimdall_schedule_round_delta(uint8_t reporting_node,
					      uint8_t observed_node, uint8_t n_nodes);
uint8_t heimdall_schedule_order(uint32_t k, uint8_t reporting_node,
					uint8_t n_nodes, uint8_t ordinal);
