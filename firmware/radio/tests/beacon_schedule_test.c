#include <assert.h>
#include <stdint.h>

#include "beacon_schedule.h"

static void assert_next_owner(uint32_t received_k, uint8_t node_id,
			      uint8_t expected_slots)
{
	uint8_t slots = heimdall_schedule_slots_until_owner(received_k, node_id, 3U);

	assert(slots == expected_slots);
	assert(heimdall_schedule_transmitter(received_k + slots, 3U) == node_id);
}

int main(void)
{
	/* All three ownership positions in an ordinary cycle. */
	assert_next_owner(0U, 1U, 1U);
	assert_next_owner(0U, 2U, 2U);
	assert_next_owner(1U, 2U, 1U);
	assert_next_owner(1U, 0U, 2U);
	assert_next_owner(2U, 0U, 1U);
	assert_next_owner(2U, 1U, 2U);

	/* Missing node 1, and missing node 2 after node 1 was received. */
	assert_next_owner(0U, 2U, 2U);
	assert_next_owner(1U, 0U, 2U);

	/* Any node can recover when its immediate predecessor is absent. */
	assert_next_owner(3U, 2U, 2U);
	assert_next_owner(4U, 0U, 2U);
	assert_next_owner(5U, 1U, 2U);

	/* Search wrapped values rather than carrying modulo state across rollover. */
	assert_next_owner(UINT32_MAX, 0U, 1U);
	assert_next_owner(UINT32_MAX, 1U, 2U);
	assert_next_owner(UINT32_MAX, 2U, 3U);

	assert(heimdall_schedule_slots_until_owner(0U, 3U, 3U) == 0U);
	assert(heimdall_schedule_slots_until_owner(0U, 0U, 0U) == 0U);

	/* N=5/M=2 derives local m=0 timing from either peer fragment. */
	assert(heimdall_schedule_frame_slots_until_owner(0U, 0U, 1U, 5U, 2U) == 2U);
	assert(heimdall_schedule_frame_slots_until_owner(0U, 1U, 1U, 5U, 2U) == 1U);
	assert(heimdall_schedule_frame_slots_until_owner(2U, 1U, 0U, 5U, 2U) == 5U);
	assert(heimdall_schedule_frame_slots_until_owner(0U, 2U, 1U, 5U, 2U) == 0U);
	return 0;
}
