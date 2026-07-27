#pragma once

#include <stdbool.h>
#include <stdint.h>

struct heimdall_usb_cycle_summary {
	uint32_t k_cycle_start;
	uint32_t cycle_index;
	uint16_t frames_received;
	uint16_t frames_expected;
	uint16_t fcs_errors;
	uint16_t filter_rejects;
	uint16_t validation_rejects;
	uint16_t subreport_crc_failures;
	uint16_t usb_queue_drops;
	uint16_t rx_callback_max_us;
	uint8_t peer_m0_miss[8];
	uint8_t evidence_age;
	uint8_t flags;
};

bool heimdall_usb_emit_hello(uint64_t device_id, bool unsynchronized);
bool heimdall_usb_emit_heartbeat(uint32_t cycles_completed, uint8_t sync_state,
				 uint8_t evidence_age);
bool heimdall_usb_emit_radio_frame(uint64_t rx_timestamp, uint8_t rx_flags,
				   const uint8_t *frame, uint16_t frame_length,
				   bool unsynchronized);
bool heimdall_usb_emit_local_observation(uint32_t k,
					 const uint8_t *subreport,
					 uint16_t subreport_length,
					 bool unsynchronized);
bool heimdall_usb_emit_tx_record(uint32_t k, uint64_t tx_timestamp,
				 uint16_t frame_length, bool confirmed,
				 bool unsynchronized);
bool heimdall_usb_emit_cycle_summary(
	const struct heimdall_usb_cycle_summary *summary, bool unsynchronized);
bool heimdall_usb_emit_error(uint16_t code, uint16_t detail, uint32_t k,
			     bool unsynchronized);
