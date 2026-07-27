#include "usb_runtime_records.h"

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>

#include "heimdall_beacon_config.h"
#include "usb_cir_stream.h"

#define USB_RECORD_HELLO 0x01U
#define USB_RECORD_HEARTBEAT 0x02U
#define USB_RECORD_RADIO_FRAME 0x03U
#define USB_RECORD_LOCAL_OBS 0x04U
#define USB_RECORD_CYCLE_SUMMARY 0x05U
#define USB_RECORD_ERROR 0x06U
#define USB_RECORD_TX 0x07U
#define USB_FLAG_UNSYNCHRONIZED BIT(1)

static void put_le40(uint8_t out[5], uint64_t value)
{
	for (uint8_t i = 0U; i < 5U; ++i) {
		out[i] = (uint8_t)(value >> (8U * i));
	}
}

static uint8_t outer_flags(bool unsynchronized)
{
	return unsynchronized ? USB_FLAG_UNSYNCHRONIZED : 0U;
}

bool heimdall_usb_emit_hello(uint64_t device_id, bool unsynchronized)
{
	uint8_t payload[36] = {0};

	payload[0] = HEIMDALL_PROTOCOL_VERSION;
	payload[1] = 1U;
	payload[2] = HEIMDALL_N_NODES;
	payload[3] = HEIMDALL_M;
	payload[4] = CONFIG_HEIMDALL_NODE_ID;
	payload[5] = HEIMDALL_MASTER_NODE_ID;
	payload[6] = HEIMDALL_CIR_TAPS;
	payload[7] = HEIMDALL_CIR_LEFT_TAPS;
	sys_put_le16(HEIMDALL_CONFIG_HASH, &payload[8]);
	sys_put_le16(HEIMDALL_SUBREPORT_BYTES, &payload[10]);
	sys_put_le16(HEIMDALL_FRAME_PAYLOAD_BYTES, &payload[12]);
	sys_put_le16(HEIMDALL_MAX_FRAME_BYTES, &payload[14]);
	sys_put_le32(HEIMDALL_SLOT_DURATION_US, &payload[16]);
	sys_put_le32(HEIMDALL_CYCLE_US, &payload[20]);
	sys_put_le64(device_id, &payload[24]);
	return heimdall_usb_enqueue_record(USB_RECORD_HELLO,
		outer_flags(unsynchronized), payload, sizeof(payload), NULL, 0U);
}

bool heimdall_usb_emit_heartbeat(uint32_t cycles_completed, uint8_t sync_state,
				 uint8_t evidence_age)
{
	uint8_t payload[12] = {0};

	sys_put_le32(k_uptime_get_32(), &payload[0]);
	sys_put_le32(cycles_completed, &payload[4]);
	payload[8] = sync_state;
	payload[9] = evidence_age;
	return heimdall_usb_enqueue_record(USB_RECORD_HEARTBEAT,
		sync_state == 1U ? 0U : USB_FLAG_UNSYNCHRONIZED,
		payload, sizeof(payload), NULL, 0U);
}

bool heimdall_usb_emit_radio_frame(uint64_t rx_timestamp, uint8_t rx_flags,
				   const uint8_t *frame, uint16_t frame_length,
				   bool unsynchronized)
{
	uint8_t prefix[8];

	put_le40(&prefix[0], rx_timestamp);
	prefix[5] = rx_flags;
	sys_put_le16(frame_length, &prefix[6]);
	return heimdall_usb_enqueue_record(USB_RECORD_RADIO_FRAME,
		outer_flags(unsynchronized), prefix, sizeof(prefix), frame,
		frame_length);
}

bool heimdall_usb_emit_local_observation(uint32_t k,
					 const uint8_t *subreport,
					 uint16_t subreport_length,
					 bool unsynchronized)
{
	uint8_t prefix[5];

	prefix[0] = CONFIG_HEIMDALL_NODE_ID;
	sys_put_le32(k, &prefix[1]);
	return heimdall_usb_enqueue_record(USB_RECORD_LOCAL_OBS,
		outer_flags(unsynchronized), prefix, sizeof(prefix), subreport,
		subreport_length);
}

bool heimdall_usb_emit_tx_record(uint32_t k, uint64_t tx_timestamp,
				 uint16_t frame_length, bool confirmed,
				 bool unsynchronized)
{
	uint8_t payload[13];

	sys_put_le32(k, &payload[0]);
	payload[4] = 0U;
	put_le40(&payload[5], tx_timestamp);
	sys_put_le16(frame_length, &payload[10]);
	payload[12] = confirmed ? BIT(0) : 0U;
	return heimdall_usb_enqueue_record(USB_RECORD_TX,
		outer_flags(unsynchronized), payload, sizeof(payload), NULL, 0U);
}

bool heimdall_usb_emit_cycle_summary(
	const struct heimdall_usb_cycle_summary *summary, bool unsynchronized)
{
	uint8_t payload[34];

	if (summary == NULL) {
		return false;
	}
	sys_put_le32(summary->k_cycle_start, &payload[0]);
	sys_put_le32(summary->cycle_index, &payload[4]);
	sys_put_le16(summary->frames_received, &payload[8]);
	sys_put_le16(summary->frames_expected, &payload[10]);
	sys_put_le16(summary->fcs_errors, &payload[12]);
	sys_put_le16(summary->filter_rejects, &payload[14]);
	sys_put_le16(summary->validation_rejects, &payload[16]);
	sys_put_le16(summary->subreport_crc_failures, &payload[18]);
	sys_put_le16(summary->usb_queue_drops, &payload[20]);
	sys_put_le16(summary->rx_callback_max_us, &payload[22]);
	memcpy(&payload[24], summary->peer_m0_miss,
	       sizeof(summary->peer_m0_miss));
	payload[32] = summary->evidence_age;
	payload[33] = summary->flags;
	return heimdall_usb_enqueue_record(USB_RECORD_CYCLE_SUMMARY,
		outer_flags(unsynchronized), payload, sizeof(payload), NULL, 0U);
}

bool heimdall_usb_emit_error(uint16_t code, uint16_t detail, uint32_t k,
			     bool unsynchronized)
{
	uint8_t payload[8];

	sys_put_le16(code, &payload[0]);
	sys_put_le16(detail, &payload[2]);
	sys_put_le32(k, &payload[4]);
	return heimdall_usb_enqueue_record(USB_RECORD_ERROR,
		outer_flags(unsynchronized), payload, sizeof(payload), NULL, 0U);
}
