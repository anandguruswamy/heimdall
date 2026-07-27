#include "beacon_runtime.h"

#include <errno.h>
#include <string.h>

#include <zephyr/drivers/hwinfo.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/printk.h>

#include <deca_device_api.h>
#include <dw3000_hw.h>

#include "heimdall_beacon_config.h"
#include "beacon_report.h"
#include "beacon_schedule.h"
#include "beacon_wire.h"
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
#include "usb_cir_stream.h"
#include "usb_runtime_records.h"
#endif

#define DWT_TIMESTAMP_MASK ((1ULL << 40) - 1ULL)
#define FRAME_DATA_BYTES (HEIMDALL_FRAME_BYTES - 2U)
#define SLOT_DTU ((uint64_t)(HEIMDALL_SLOT_DURATION_US / 100U) * 6389760ULL)
#define BOOTSTRAP_LISTEN_MS 50U
/* Start fallback preparation just after the prior slot's frame should end. */
#define FALLBACK_PREP_LEAD_US (HEIMDALL_SLOT_DURATION_US - 1000U)
#define FRAME_COMPLETION_BUDGET_US 1000U
#define TX_COMPLETION_TIMEOUT_US (HEIMDALL_SLOT_DURATION_US + 2000U)
#define EPOCH_EXPIRY_MS (((HEIMDALL_EVIDENCE_AGE_THRESHOLD + 2U) * HEIMDALL_CYCLE_US) / 1000U)
#define CONFIG_MISMATCH_LIMIT 3U
#define CONFIG_RECOVERY_MATCHES 3U
#define FRAME_FLAG_BOOTSTRAP BIT(1)
#define OBS_FLAG_CIR_VALID BIT(0)
#define OBS_FLAG_CIR_TRUNCATED BIT(1)
#define OBS_FLAG_FP_VALID BIT(2)
#define GATEWAY_RX_EVENT_COUNT 16U

BUILD_ASSERT(HEIMDALL_N_NODES >= 2U && HEIMDALL_N_NODES <= 8U,
	     "the beacon runtime supports two through eight nodes");
BUILD_ASSERT(HEIMDALL_M >= 1U && HEIMDALL_M <= 3U,
	     "the beacon runtime supports one through three frames per superslot");
BUILD_ASSERT(HEIMDALL_FRAME_HEADER_BYTES + HEIMDALL_FRAME_PAYLOAD_BYTES ==
	     FRAME_DATA_BYTES);
BUILD_ASSERT(HEIMDALL_SUBREPORT_BYTES <= HEIMDALL_FRAME_PAYLOAD_BYTES);
BUILD_ASSERT(HEIMDALL_CIR_TAPS <= DWT_CIR_LEN_IP_PRF64);

volatile struct heimdall_runtime_counters heimdall_runtime_counters;

static struct {
	bool synchronized;
	bool tx_armed;
	bool identity_collision;
	bool configuration_inhibited;
	bool have_rx_k;
	bool have_tx_k;
	uint8_t mac_seq;
	uint8_t last_rx_m;
	uint8_t last_tx_m;
	uint8_t fallback_m;
	uint8_t evidence_age;
	uint8_t evidence_min_received;
	bool have_cycle_evidence;
	bool master_heard_this_cycle;
	bool evidence_target_valid;
	uint32_t evidence_target_k;
	uint32_t last_rx_k;
	uint32_t last_tx_k;
	uint32_t tx_generation;
	uint64_t last_programmed_tx_timestamp;
	uint32_t fallback_k;
	uint64_t fallback_timestamp;
	bool fallback_valid;
	uint32_t tx_report_k;
	uint32_t prepared_report_k;
	uint8_t tx_report_evidence_age;
	uint8_t tx_report_flags;
	bool tx_report_valid;
	bool prepared_report_valid;
	uint32_t last_valid_uptime_ms;
	uint8_t config_mismatch_streak;
	uint8_t config_match_streak;
} runtime;

static uint8_t rx_frame[HEIMDALL_FRAME_BYTES];
static uint8_t tx_frame[FRAME_DATA_BYTES];
static uint8_t cir_raw[HEIMDALL_CIR_TAPS * 6U];
static int16_t cir_iq[2U * HEIMDALL_CIR_TAPS];
static uint8_t subreport_bytes[HEIMDALL_REPORT_NODE_SLOTS][HEIMDALL_SUBREPORT_BYTES];
static uint32_t subreport_observed_k[HEIMDALL_REPORT_NODE_SLOTS];
static const uint8_t *subreport_ptrs[HEIMDALL_REPORT_NODE_SLOTS];
static uint16_t subreport_lengths[HEIMDALL_REPORT_NODE_SLOTS];
static struct heimdall_report report;
static struct k_work_delayable bootstrap_work;
static struct k_work_delayable watchdog_work;
struct tx_timeout_context {
	struct k_work_delayable work;
	uint32_t generation;
};
static struct tx_timeout_context tx_timeout_contexts[HEIMDALL_M];
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
enum gateway_event_type {
	GATEWAY_EVENT_RX,
	GATEWAY_EVENT_TX,
	GATEWAY_EVENT_SUMMARY,
	GATEWAY_EVENT_ERROR,
	GATEWAY_EVENT_HELLO,
	GATEWAY_EVENT_HEARTBEAT,
};

struct gateway_rx_event {
	void *fifo_reserved;
	enum gateway_event_type type;
	bool unsynchronized;
	uint64_t rx_timestamp;
	uint32_t observation_k;
	uint16_t frame_length;
	uint16_t observation_length;
	uint8_t rx_flags;
	uint8_t frame[HEIMDALL_FRAME_BYTES];
	uint8_t observation[HEIMDALL_SUBREPORT_BYTES];
	union {
		struct {
			uint32_t k;
			uint64_t timestamp;
			uint16_t frame_length;
			uint8_t m;
			bool confirmed;
		} tx;
		struct {
			struct heimdall_usb_cycle_summary value;
			uint32_t drop_ack;
		} summary;
		struct {
			uint16_t code;
			uint16_t detail;
			uint32_t k;
		} error;
		struct {
			uint32_t cycles_completed;
			uint8_t sync_state;
			uint8_t evidence_age;
		} heartbeat;
		uint64_t hello_device_id;
	};
};

K_MEM_SLAB_DEFINE(gateway_rx_event_slab, sizeof(struct gateway_rx_event),
		  GATEWAY_RX_EVENT_COUNT, 4);
K_FIFO_DEFINE(gateway_rx_event_fifo);

static struct k_work_delayable gateway_status_work;
static uint64_t gateway_device_id;
static uint32_t cycle_callback_max_us;
static uint32_t summary_rx_validated;
static uint32_t summary_rx_fcs_errors;
static uint32_t summary_filter_rejects;
static uint32_t summary_validation_rejects;
static uint32_t summary_subreport_crc_failures;
static uint32_t summary_k_cycle_start;
static bool summary_peer_received[HEIMDALL_REPORT_NODE_SLOTS];
static bool gateway_emit_hello_next;
static bool summary_cycle_active;

static void gateway_export_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (true) {
		struct gateway_rx_event *event = k_fifo_get(
			&gateway_rx_event_fifo, K_FOREVER);

		switch (event->type) {
		case GATEWAY_EVENT_RX:
			(void)heimdall_usb_emit_radio_frame(
				event->rx_timestamp, event->rx_flags, event->frame,
				event->frame_length, event->unsynchronized);
			if (event->observation_length != 0U) {
				(void)heimdall_usb_emit_local_observation(
					event->observation_k, event->observation,
					event->observation_length,
					event->unsynchronized);
			}
			break;
		case GATEWAY_EVENT_TX:
			(void)heimdall_usb_emit_tx_record(
				event->tx.k, event->tx.m, event->tx.timestamp,
				event->tx.frame_length, event->tx.confirmed,
				event->unsynchronized);
			break;
		case GATEWAY_EVENT_SUMMARY:
			if (heimdall_usb_emit_cycle_summary(
				    &event->summary.value, event->unsynchronized)) {
				/* The count was atomically claimed when queued. */
			} else {
				heimdall_usb_drop_count_restore(
					event->summary.drop_ack);
			}
			break;
		case GATEWAY_EVENT_ERROR:
			(void)heimdall_usb_emit_error(
				event->error.code, event->error.detail,
				event->error.k, event->unsynchronized);
			break;
		case GATEWAY_EVENT_HELLO:
			(void)heimdall_usb_emit_hello(
				event->hello_device_id, event->unsynchronized);
			break;
		case GATEWAY_EVENT_HEARTBEAT:
			(void)heimdall_usb_emit_heartbeat(
				event->heartbeat.cycles_completed,
				event->heartbeat.sync_state,
				event->heartbeat.evidence_age);
			break;
		}
		k_mem_slab_free(&gateway_rx_event_slab, event);
	}
}

K_THREAD_DEFINE(gateway_export_tid, 2048, gateway_export_thread,
		NULL, NULL, NULL, 4, 0, 0);

static struct gateway_rx_event *gateway_event_alloc(enum gateway_event_type type,
						      bool unsynchronized)
{
	struct gateway_rx_event *event;

	if (k_mem_slab_alloc(&gateway_rx_event_slab, (void **)&event,
			     K_NO_WAIT) != 0) {
		heimdall_usb_note_drop();
		return NULL;
	}
	event->type = type;
	event->unsynchronized = unsynchronized;
	return event;
}

static void gateway_event_submit(struct gateway_rx_event *event)
{
	if (event != NULL) {
		k_fifo_put(&gateway_rx_event_fifo, event);
	}
}

static void gateway_queue_error(uint16_t code, uint16_t detail, uint32_t k,
				bool unsynchronized)
{
	struct gateway_rx_event *event = gateway_event_alloc(
		GATEWAY_EVENT_ERROR, unsynchronized);

	if (event != NULL) {
		event->error.code = code;
		event->error.detail = detail;
		event->error.k = k;
		gateway_event_submit(event);
	}
}

static void gateway_queue_tx(uint32_t k, uint8_t m, uint64_t timestamp,
			     bool confirmed, bool unsynchronized)
{
	struct gateway_rx_event *event = gateway_event_alloc(
		GATEWAY_EVENT_TX, unsynchronized);

	if (event != NULL) {
		event->tx.k = k;
		event->tx.m = m;
		event->tx.timestamp = timestamp;
		event->tx.frame_length = HEIMDALL_FRAME_BYTES;
		event->tx.confirmed = confirmed;
		gateway_event_submit(event);
	}
}

static void gateway_submit_rx(struct gateway_rx_event **event_ptr,
			      bool *submitted, uint64_t rx_timestamp,
			      uint8_t rx_flags, uint16_t captured_length,
			      uint32_t observation_k,
			      const uint8_t *observation,
			      uint16_t observation_length,
			      bool unsynchronized)
{
	struct gateway_rx_event *event = *event_ptr;

	if (event == NULL) {
		return;
	}
	event->type = GATEWAY_EVENT_RX;
	event->rx_timestamp = rx_timestamp;
	event->rx_flags = rx_flags;
	event->frame_length = captured_length >= 2U ? captured_length - 2U : 0U;
	event->unsynchronized = unsynchronized;
	event->observation_length = observation_length;
	if (observation_length != 0U) {
		event->observation_k = observation_k;
		memcpy(event->observation, observation, observation_length);
	}
	gateway_event_submit(event);
	*event_ptr = NULL;
	*submitted = true;
}
#endif

static uint64_t timestamp40(const uint8_t timestamp[5])
{
	uint64_t value = 0U;

	for (int i = 4; i >= 0; --i) {
		value = (value << 8) | timestamp[i];
	}
	return value;
}

static uint64_t read_system_timestamp40(void)
{
	uint8_t timestamp[4];
	uint64_t value = 0U;

	dwt_readsystime(timestamp);
	for (int i = 3; i >= 0; --i) {
		value = (value << 8) | timestamp[i];
	}
	return (value << 8) & DWT_TIMESTAMP_MASK;
}

static int64_t timestamp40_diff(uint64_t actual, uint64_t expected)
{
	uint64_t delta = (actual - expected) & DWT_TIMESTAMP_MASK;

	if ((delta & (1ULL << 39)) != 0U) {
		return (int64_t)(delta - (1ULL << 40));
	}
	return (int64_t)delta;
}

static bool k_is_newer(uint32_t candidate, uint32_t previous)
{
	return (int32_t)(candidate - previous) > 0;
}

static uint8_t slots_until_local_owner(uint32_t k, uint8_t m)
{
	return heimdall_schedule_frame_slots_until_owner(
		k, m, CONFIG_HEIMDALL_NODE_ID, HEIMDALL_N_NODES, HEIMDALL_M);
}

static void schedule_fallback(uint32_t k, uint8_t m, uint64_t timestamp,
			      uint32_t delay_us)
{
	runtime.fallback_k = k;
	runtime.fallback_m = m;
	runtime.fallback_timestamp = timestamp;
	runtime.fallback_valid = true;
	(void)k_work_reschedule(&watchdog_work, K_USEC(delay_us));
}

static void schedule_after_frame(uint32_t prior_k, uint8_t prior_m,
				 uint64_t prior_timestamp, uint32_t elapsed_us)
{
	uint32_t target_k;
	uint8_t target_m;
	uint8_t slots;
	uint32_t delay_us;

	if (prior_m + 1U < HEIMDALL_M) {
		target_k = prior_k;
		target_m = prior_m + 1U;
		slots = 1U;
	} else {
		slots = slots_until_local_owner(prior_k, prior_m);
		target_k = prior_k + heimdall_schedule_slots_until_owner(
			prior_k, CONFIG_HEIMDALL_NODE_ID, HEIMDALL_N_NODES);
		target_m = 0U;
	}
	delay_us = slots * HEIMDALL_SLOT_DURATION_US;
	delay_us = delay_us > elapsed_us + FALLBACK_PREP_LEAD_US ?
		delay_us - elapsed_us - FALLBACK_PREP_LEAD_US : 0U;
	schedule_fallback(target_k, target_m,
		(prior_timestamp + slots * SLOT_DTU) & DWT_TIMESTAMP_MASK,
		delay_us);
}

static void update_evidence(const struct heimdall_frame_header *header)
{
	uint8_t superslots = heimdall_schedule_slots_until_owner(
		header->k, CONFIG_HEIMDALL_NODE_ID, HEIMDALL_N_NODES);
	uint32_t target_k = header->k + superslots;

	if (!runtime.evidence_target_valid || runtime.evidence_target_k != target_k) {
		runtime.have_cycle_evidence = false;
		runtime.master_heard_this_cycle = false;
		runtime.evidence_min_received = UINT8_MAX;
		runtime.evidence_target_k = target_k;
		runtime.evidence_target_valid = true;
	}
	runtime.have_cycle_evidence = true;
	if (header->src_addr == HEIMDALL_MASTER_NODE_ID) {
		runtime.master_heard_this_cycle = true;
		runtime.evidence_age = 0U;
	} else if (!runtime.master_heard_this_cycle) {
		runtime.evidence_min_received =
			MIN(runtime.evidence_min_received, header->evidence_age);
		runtime.evidence_age = runtime.evidence_min_received == UINT8_MAX ?
			UINT8_MAX : runtime.evidence_min_received + 1U;
	}
}

static void prepare_transmit_evidence(uint32_t k)
{
	if (CONFIG_HEIMDALL_NODE_ID == HEIMDALL_MASTER_NODE_ID) {
		runtime.evidence_age = 0U;
	} else if (!runtime.have_cycle_evidence || !runtime.evidence_target_valid ||
		   runtime.evidence_target_k != k) {
		runtime.evidence_age = UINT8_MAX;
	}
}

static int prepare_transmit_report(uint32_t k)
{
	int ret;

	for (uint8_t peer = 0U; peer < HEIMDALL_N_NODES; ++peer) {
		uint8_t delta = heimdall_schedule_round_delta(
			CONFIG_HEIMDALL_NODE_ID, peer, HEIMDALL_N_NODES);

		if (peer != CONFIG_HEIMDALL_NODE_ID &&
		    subreport_ptrs[peer] != NULL &&
		    subreport_observed_k[peer] != k - delta) {
			subreport_ptrs[peer] = NULL;
			subreport_lengths[peer] = 0U;
		}
	}
	ret = heimdall_report_pack(&report, k, CONFIG_HEIMDALL_NODE_ID,
				   HEIMDALL_N_NODES, subreport_ptrs,
				   subreport_lengths);
	if (ret != 0) {
		heimdall_runtime_counters.report_assembly_failures++;
		runtime.prepared_report_valid = false;
		return ret;
	}
	runtime.prepared_report_k = k;
	runtime.prepared_report_valid = true;
	return 0;
}

static int32_t sign_extend_18(const uint8_t value[3])
{
	int32_t sample = (int32_t)value[0] |
			 ((int32_t)value[1] << 8) |
			 (((int32_t)value[2] & 0x03) << 16);

	if ((sample & 0x20000) != 0) {
		sample |= ~0x3FFFF;
	}
	return sample;
}

static void update_counter_state(void)
{
	heimdall_runtime_counters.synchronized = runtime.synchronized;
	heimdall_runtime_counters.tx_armed = runtime.tx_armed;
	heimdall_runtime_counters.evidence_age = runtime.evidence_age;
}

static void rearm_rx(void)
{
	if (!runtime.tx_armed) {
		(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
	}
}

#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
static uint16_t saturating_delta(uint32_t current, uint32_t previous,
				 uint8_t *flags)
{
	uint32_t delta = current - previous;

	if (delta > UINT16_MAX) {
		*flags |= BIT(0);
		return UINT16_MAX;
	}
	return (uint16_t)delta;
}

static uint32_t validation_reject_total(void)
{
	return heimdall_runtime_counters.reject_length +
		heimdall_runtime_counters.reject_header +
		heimdall_runtime_counters.reject_config +
		heimdall_runtime_counters.reject_schedule +
		heimdall_runtime_counters.reject_stale +
		heimdall_runtime_counters.reject_subreport -
		heimdall_runtime_counters.subreport_crc_failures;
}

static void cycle_summary_handler(struct k_work *work);

static void gateway_begin_cycle(uint32_t k)
{
	summary_k_cycle_start = k - CONFIG_HEIMDALL_NODE_ID;
	summary_rx_validated = heimdall_runtime_counters.rx_validated;
	summary_rx_fcs_errors = heimdall_runtime_counters.rx_fcs_errors;
	summary_filter_rejects = heimdall_runtime_counters.rx_filter_rejects;
	summary_validation_rejects = validation_reject_total();
	summary_subreport_crc_failures =
		heimdall_runtime_counters.subreport_crc_failures;
	cycle_callback_max_us = 0U;
	memset(summary_peer_received, 0, sizeof(summary_peer_received));
	summary_cycle_active = true;
}
#endif

static int build_and_schedule(uint32_t k, uint8_t m,
			      uint64_t desired_programmed_timestamp, bool watchdog)
{
	struct heimdall_frame_header header;
	uint32_t delayed_time;
	uint64_t raw_target;
	uint64_t programmed;
	uint64_t system_timestamp;
	int ret;

	if (runtime.identity_collision || runtime.configuration_inhibited ||
	    runtime.tx_armed ||
	    m >= HEIMDALL_M ||
	    heimdall_schedule_transmitter(k, HEIMDALL_N_NODES) !=
		CONFIG_HEIMDALL_NODE_ID) {
		return -EPERM;
	}
	if (m == 0U) {
		prepare_transmit_evidence(k);
	}
	if (CONFIG_HEIMDALL_NODE_ID != HEIMDALL_MASTER_NODE_ID &&
	    (m == 0U ? runtime.evidence_age : runtime.tx_report_evidence_age) >
		HEIMDALL_EVIDENCE_AGE_THRESHOLD) {
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0006U, runtime.evidence_age, k,
				    !runtime.synchronized);
#endif
		return -EHOSTDOWN;
	}
	raw_target = (desired_programmed_timestamp -
		      CONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU) &
		     DWT_TIMESTAMP_MASK;
	delayed_time = (uint32_t)(raw_target >> 8);
	programmed = ((((uint64_t)(delayed_time & 0xFFFFFFFEUL)) << 8) +
		      CONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU) & DWT_TIMESTAMP_MASK;

	if (m == 0U) {
		if (!runtime.prepared_report_valid || runtime.prepared_report_k != k) {
			ret = prepare_transmit_report(k);
			if (ret != 0) {
				return ret;
			}
		}
		runtime.tx_report_k = k;
		runtime.tx_report_evidence_age =
			CONFIG_HEIMDALL_NODE_ID == HEIMDALL_MASTER_NODE_ID ?
			0U : runtime.evidence_age;
		runtime.tx_report_flags = runtime.synchronized ? 0U : FRAME_FLAG_BOOTSTRAP;
		runtime.tx_report_valid = true;
	} else if (!runtime.tx_report_valid || runtime.tx_report_k != k) {
		return -ENODATA;
	}

	header = (struct heimdall_frame_header) {
		.mac_seq = runtime.mac_seq++,
		.network_id = HEIMDALL_NETWORK_ID,
		.src_addr = CONFIG_HEIMDALL_NODE_ID,
		.protocol_version = HEIMDALL_PROTOCOL_VERSION,
		.frame_type = 0U,
		.m = m,
		.k = k,
		.n_nodes = HEIMDALL_N_NODES,
		.slots_per_superslot = HEIMDALL_M,
		.config_hash = HEIMDALL_CONFIG_HASH,
		.tx_timestamp = programmed,
		.subreport_count = heimdall_report_frame_subreport_count(
			&report, m, HEIMDALL_FRAME_PAYLOAD_BYTES),
		.pooled_total_bytes = report.total_bytes,
		.peer_observed_bitmap = report.peer_observed_bitmap,
		.evidence_age = runtime.tx_report_evidence_age,
		.flags = runtime.tx_report_flags,
	};

	ret = heimdall_frame_header_encode(&header, tx_frame, sizeof(tx_frame));
	if (ret == 0) {
		ret = heimdall_report_copy_frame(
			&report, m, HEIMDALL_FRAME_PAYLOAD_BYTES,
			&tx_frame[HEIMDALL_FRAME_HEADER_BYTES],
			HEIMDALL_FRAME_PAYLOAD_BYTES);
	}
	if (ret != 0) {
		heimdall_runtime_counters.report_assembly_failures++;
		return ret;
	}

	dwt_forcetrxoff();
	dwt_writetxdata(FRAME_DATA_BYTES, tx_frame, 0U);
	dwt_writetxfctrl(HEIMDALL_FRAME_BYTES, 0U, 0U);
	dwt_setdelayedtrxtime(delayed_time);
	system_timestamp = read_system_timestamp40();
	heimdall_runtime_counters.last_programmed_tx_timestamp = programmed;
	heimdall_runtime_counters.last_tx_error_dtu = timestamp40_diff(
		programmed, system_timestamp);
	if (heimdall_runtime_counters.tx_attempted == 0U) {
		heimdall_runtime_counters.first_tx_system_timestamp =
			system_timestamp;
		heimdall_runtime_counters.first_programmed_tx_timestamp = programmed;
		heimdall_runtime_counters.first_tx_lead_dtu =
			heimdall_runtime_counters.last_tx_error_dtu;
	}
	dwt_writesysstatuslo(DWT_INT_HPDWARN_BIT_MASK);
	heimdall_runtime_counters.tx_attempted++;
	if (dwt_starttx(DWT_START_TX_DELAYED) == DWT_ERROR) {
		heimdall_runtime_counters.tx_start_late++;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0007U, m, k, !runtime.synchronized);
#endif
		rearm_rx();
		return -ETIME;
	}

	if (m == 0U) {
		memset(subreport_ptrs, 0, sizeof(subreport_ptrs));
		memset(subreport_lengths, 0, sizeof(subreport_lengths));
		runtime.prepared_report_valid = false;
	}
	runtime.tx_armed = true;
	runtime.fallback_valid = false;
	runtime.have_tx_k = true;
	runtime.last_tx_k = k;
	runtime.last_tx_m = m;
	runtime.tx_generation++;
	tx_timeout_contexts[m].generation = runtime.tx_generation;
	runtime.last_programmed_tx_timestamp = programmed;
	(void)k_work_reschedule(&tx_timeout_contexts[m].work,
				 K_USEC(TX_COMPLETION_TIMEOUT_US));
	heimdall_runtime_counters.last_tx_k = k;
	heimdall_runtime_counters.last_programmed_tx_timestamp = programmed;
	if (watchdog) {
		heimdall_runtime_counters.watchdog_transmissions++;
	}
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	if (m == 0U) {
		if (summary_cycle_active) {
			cycle_summary_handler(NULL);
		}
		gateway_begin_cycle(k);
	}
#endif
	if (m == 0U) {
		runtime.have_cycle_evidence = false;
		runtime.master_heard_this_cycle = false;
		runtime.evidence_min_received = UINT8_MAX;
		runtime.evidence_target_valid = false;
	}
	update_counter_state();
	return 0;
}

static int validate_frame_header(const uint8_t *frame, uint16_t length,
				 struct heimdall_frame_header *header,
				 uint8_t *usb_rx_flags)
{
	if (length != HEIMDALL_FRAME_BYTES) {
		heimdall_runtime_counters.reject_length++;
		return -EMSGSIZE;
	}
	if (frame[9] != HEIMDALL_PROTOCOL_VERSION) {
		*usb_rx_flags |= BIT(2);
	}
	if (heimdall_frame_header_decode(header, frame,
					 FRAME_DATA_BYTES) != 0 ||
	    sys_get_le16(&frame[7]) != header->src_addr ||
	    header->frame_type != 0U || header->m >= HEIMDALL_M ||
	    (header->flags & ~FRAME_FLAG_BOOTSTRAP) != 0U) {
		heimdall_runtime_counters.reject_header++;
		return -EBADMSG;
	}
	if (header->src_addr >= HEIMDALL_N_NODES ||
	    heimdall_schedule_transmitter(header->k, HEIMDALL_N_NODES) !=
		header->src_addr) {
		heimdall_runtime_counters.reject_schedule++;
		return -ERANGE;
	}
	if (runtime.have_rx_k &&
	    !(k_is_newer(header->k, runtime.last_rx_k) ||
	      (header->k == runtime.last_rx_k && header->m > runtime.last_rx_m)) &&
	    (uint32_t)(k_uptime_get_32() - runtime.last_valid_uptime_ms) <
		EPOCH_EXPIRY_MS) {
		heimdall_runtime_counters.reject_stale++;
		return -EALREADY;
	}

	if (header->network_id != HEIMDALL_NETWORK_ID ||
	    header->protocol_version != HEIMDALL_PROTOCOL_VERSION ||
	    header->n_nodes != HEIMDALL_N_NODES ||
	    header->slots_per_superslot != HEIMDALL_M ||
	    header->config_hash != HEIMDALL_CONFIG_HASH) {
		if (header->config_hash != HEIMDALL_CONFIG_HASH) {
			*usb_rx_flags |= BIT(1);
		}
		heimdall_runtime_counters.reject_config++;
		runtime.config_match_streak = 0U;
		if (runtime.config_mismatch_streak < UINT8_MAX) {
			runtime.config_mismatch_streak++;
		}
		if (runtime.config_mismatch_streak >= CONFIG_MISMATCH_LIMIT &&
		    !runtime.configuration_inhibited) {
			runtime.configuration_inhibited = true;
			heimdall_runtime_counters.configuration_inhibitions++;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
			gateway_queue_error(0x0005U, header->config_hash,
					    header->k, !runtime.synchronized);
#endif
		}
		return -EPROTO;
	}
	runtime.config_mismatch_streak = 0U;
	if (runtime.config_match_streak < CONFIG_RECOVERY_MATCHES) {
		runtime.config_match_streak++;
	}
	if (runtime.config_match_streak >= CONFIG_RECOVERY_MATCHES) {
		runtime.configuration_inhibited = false;
	}
	if (header->src_addr == CONFIG_HEIMDALL_NODE_ID) {
		runtime.identity_collision = true;
		heimdall_runtime_counters.identity_collisions++;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0004U, header->src_addr, header->k,
				    !runtime.synchronized);
#endif
		return -EADDRINUSE;
	}
	if (header->subreport_count > HEIMDALL_N_NODES - 1U ||
	    header->pooled_total_bytes > (HEIMDALL_N_NODES - 1U) *
		HEIMDALL_SUBREPORT_BYTES ||
	    (header->peer_observed_bitmap & ~((1U << HEIMDALL_N_NODES) - 1U)) != 0U ||
	    (header->peer_observed_bitmap & BIT(header->src_addr)) != 0U ||
	    ((header->pooled_total_bytes == 0U) !=
	     (header->peer_observed_bitmap == 0U))) {
		heimdall_runtime_counters.reject_subreport++;
		return -EBADMSG;
	}
	*usb_rx_flags |= BIT(0);
	return 0;
}

static int make_observation(const struct heimdall_frame_header *header,
			    uint64_t rx_timestamp, uint8_t rx_flags)
{
	dwt_cirdiags_t diag;
	struct heimdall_subreport subreport;
	uint16_t fp_index;
	uint16_t cir_start;
	uint16_t cir_taps;

	if ((rx_flags & DWT_CB_DATA_RX_FLAG_CIA) == 0U ||
	    (rx_flags & DWT_CB_DATA_RX_FLAG_CER) != 0U) {
		heimdall_runtime_counters.diagnostic_failures++;
		return -EIO;
	}

	if (dwt_readdiagnostics_acc(&diag, DWT_ACC_IDX_IP_M) != DWT_SUCCESS) {
		heimdall_runtime_counters.diagnostic_failures++;
		return -EIO;
	}
	fp_index = (uint16_t)((diag.FpIndex + 32U) >> 6);
	cir_start = fp_index > HEIMDALL_CIR_LEFT_TAPS ?
		fp_index - HEIMDALL_CIR_LEFT_TAPS : 0U;
	if (cir_start >= DWT_CIR_LEN_IP_PRF64) {
		heimdall_runtime_counters.diagnostic_failures++;
		return -ERANGE;
	}
	cir_taps = MIN(HEIMDALL_CIR_TAPS,
		       DWT_CIR_LEN_IP_PRF64 - cir_start);
	dwt_readcir_48b(cir_raw, DWT_ACC_IDX_IP_M, cir_start,
			 cir_taps);
	heimdall_runtime_counters.cir_reads++;
	for (uint16_t i = 0U; i < cir_taps; ++i) {
		cir_iq[2U * i] = (int16_t)(sign_extend_18(&cir_raw[i * 6U]) >> 2);
		cir_iq[2U * i + 1U] =
			(int16_t)(sign_extend_18(&cir_raw[i * 6U + 3U]) >> 2);
	}

	subreport = (struct heimdall_subreport) {
		.observed_node_id = header->src_addr,
		.obs_flags = OBS_FLAG_CIR_VALID | OBS_FLAG_FP_VALID,
		.observed_m = 0U,
		.round_delta = heimdall_schedule_round_delta(
			CONFIG_HEIMDALL_NODE_ID, header->src_addr,
			HEIMDALL_N_NODES),
		.observed_tx_timestamp = header->tx_timestamp,
		.rx_timestamp = rx_timestamp,
		.cfo_raw = dwt_readclockoffset(),
		.fp_index_q10_6 = diag.FpIndex,
		.f1 = diag.F1,
		.f2 = diag.F2,
		.f3 = diag.F3,
		.ip_power = diag.power,
		.accum_count = diag.accumCount,
		.dgc_decision = dwt_get_dgcdecision(),
		.cir_start_offset = cir_start,
		.cir_taps = cir_taps,
		.cir_iq = cir_iq,
	};
	if ((cir_start == 0U && fp_index < HEIMDALL_CIR_LEFT_TAPS) ||
	    cir_taps < HEIMDALL_CIR_TAPS) {
		subreport.obs_flags |= OBS_FLAG_CIR_TRUNCATED;
	}
	if (heimdall_subreport_encode(&subreport,
				      subreport_bytes[header->src_addr],
				      sizeof(subreport_bytes[header->src_addr])) != 0) {
		heimdall_runtime_counters.subreport_encode_failures++;
		return -EIO;
	}
	subreport_ptrs[header->src_addr] = subreport_bytes[header->src_addr];
	subreport_lengths[header->src_addr] =
		HEIMDALL_SUBREPORT_FIXED_BYTES + 4U * cir_taps;
	subreport_observed_k[header->src_addr] = header->k;
	return 0;
}

static void rx_ok_cb(const dwt_cb_data_t *cb_data)
{
	struct heimdall_frame_header header;
	uint8_t rx_timestamp_bytes[5];
	uint64_t rx_timestamp;
	uint32_t callback_start = k_cycle_get_32();
	uint32_t next_k;
	uint64_t next_timestamp;
	uint32_t elapsed;
	uint8_t slots_to_tx;
	uint16_t captured_length = MIN(cb_data->datalength, sizeof(rx_frame));
	uint16_t observation_length = 0U;
	uint8_t usb_rx_flags = 0U;
	uint8_t *frame_data = rx_frame;
	int validation_result;
	int observation_result = -ENODATA;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	struct gateway_rx_event *gateway_event = NULL;
	bool gateway_cycle_started = false;
	bool gateway_event_submitted = false;

	if (k_mem_slab_alloc(&gateway_rx_event_slab,
			     (void **)&gateway_event, K_NO_WAIT) == 0) {
		frame_data = gateway_event->frame;
	}
#endif

	heimdall_runtime_counters.rx_frames++;
	dwt_readrxdata(frame_data, captured_length, 0U);
	dwt_readrxtimestamp(rx_timestamp_bytes, DWT_IP_M);
	rx_timestamp = timestamp40(rx_timestamp_bytes);
	if (cb_data->datalength > sizeof(rx_frame)) {
		usb_rx_flags |= BIT(3);
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0008U, cb_data->datalength, 0U,
				    !runtime.synchronized);
#endif
	}
	validation_result = validate_frame_header(frame_data, cb_data->datalength, &header,
						  &usb_rx_flags);
	if (validation_result != 0) {
		rearm_rx();
		goto out;
	}
	heimdall_runtime_counters.rx_validated++;
	runtime.have_rx_k = true;
	runtime.last_rx_k = header.k;
	runtime.last_rx_m = header.m;
	runtime.last_valid_uptime_ms = k_uptime_get_32();
	runtime.synchronized = true;
	update_evidence(&header);
	heimdall_runtime_counters.last_rx_k = header.k;
	heimdall_runtime_counters.last_rx_timestamp = rx_timestamp;
	heimdall_runtime_counters.peer_adoptions++;
	(void)k_work_cancel_delayable(&bootstrap_work);
	update_counter_state();
	observation_result = header.m == 0U ?
		make_observation(&header, rx_timestamp, cb_data->rx_flags) : -ENODATA;
	if (observation_result == 0) {
		observation_length = subreport_lengths[header.src_addr];
	}
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	if (header.m == 0U) {
		summary_peer_received[header.src_addr] = true;
	}
#endif

	slots_to_tx = slots_until_local_owner(header.k, header.m);
	next_k = header.k + heimdall_schedule_slots_until_owner(
		header.k, CONFIG_HEIMDALL_NODE_ID, HEIMDALL_N_NODES);
	if (slots_to_tx > 1U) {
		rearm_rx();
	}
	if (header.m == 0U || !runtime.prepared_report_valid ||
	    runtime.prepared_report_k != next_k) {
		(void)prepare_transmit_report(next_k);
	}
	next_timestamp = (rx_timestamp + slots_to_tx * SLOT_DTU) &
		DWT_TIMESTAMP_MASK;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	gateway_submit_rx(&gateway_event, &gateway_event_submitted,
		rx_timestamp, usb_rx_flags, captured_length,
		observation_length != 0U ? header.k : 0U,
		observation_length != 0U ? subreport_bytes[header.src_addr] : NULL,
		observation_length, !runtime.synchronized);
#endif
	if (slots_to_tx == 1U) {
		int schedule_result;

#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		uint32_t partial_elapsed = k_cyc_to_us_floor32(
			k_cycle_get_32() - callback_start);

		if (partial_elapsed > cycle_callback_max_us) {
			cycle_callback_max_us = partial_elapsed;
		}
#endif
		(void)k_work_cancel_delayable(&watchdog_work);
		schedule_result = build_and_schedule(next_k, 0U, next_timestamp, false);
		if (schedule_result != 0) {
			rearm_rx();
			if (schedule_result == -ETIME) {
				schedule_after_frame(next_k, 0U, next_timestamp, 0U);
			}
		}
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_cycle_started = schedule_result == 0;
#endif
	} else {
		uint32_t partial_elapsed = k_cyc_to_us_floor32(
			k_cycle_get_32() - callback_start);
		uint32_t fallback_window =
			slots_to_tx * HEIMDALL_SLOT_DURATION_US;
		uint32_t consumed = FALLBACK_PREP_LEAD_US +
			FRAME_COMPLETION_BUDGET_US + partial_elapsed;

		schedule_fallback(next_k, 0U, next_timestamp,
			fallback_window > consumed ? fallback_window - consumed : 0U);
	}
#if !defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	ARG_UNUSED(observation_result);
#endif
out:
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	gateway_submit_rx(&gateway_event, &gateway_event_submitted,
		rx_timestamp, usb_rx_flags, captured_length,
		observation_length != 0U ? header.k : 0U,
		observation_length != 0U ? subreport_bytes[header.src_addr] : NULL,
		observation_length, !runtime.synchronized);
	if (!gateway_event_submitted) {
		heimdall_usb_note_drop();
		if (observation_length != 0U) {
			heimdall_usb_note_drop();
		}
	}
#endif
	elapsed = k_cyc_to_us_floor32(k_cycle_get_32() - callback_start);
	if (elapsed > heimdall_runtime_counters.callback_max_us) {
		heimdall_runtime_counters.callback_max_us = elapsed;
	}
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	if (!gateway_cycle_started && elapsed > cycle_callback_max_us) {
		cycle_callback_max_us = elapsed;
	}
#endif
}

static void rx_error_cb(const dwt_cb_data_t *cb_data)
{
	heimdall_runtime_counters.rx_errors++;
	if ((cb_data->status & DWT_INT_RXFCE_BIT_MASK) != 0U) {
		heimdall_runtime_counters.rx_fcs_errors++;
	}
	if ((cb_data->status & DWT_INT_ARFE_BIT_MASK) != 0U) {
		heimdall_runtime_counters.rx_filter_rejects++;
	}
	rearm_rx();
}

static void tx_done_cb(const dwt_cb_data_t *cb_data)
{
	uint8_t timestamp[5];
	int64_t error;
	int next_result = -ENODATA;

	ARG_UNUSED(cb_data);
	/* A timeout may have won the boundary race while TXFRS was pending. */
	if (!runtime.tx_armed) {
		return;
	}
	(void)k_work_cancel_delayable(
		&tx_timeout_contexts[runtime.last_tx_m].work);
	dwt_readtxtimestamp(timestamp);
	error = timestamp40_diff(timestamp40(timestamp),
				 runtime.last_programmed_tx_timestamp);
	heimdall_runtime_counters.tx_completed++;
	heimdall_runtime_counters.last_tx_error_dtu = error;
	if (error != 0) {
		heimdall_runtime_counters.tx_timestamp_errors++;
	}
	runtime.tx_armed = false;
	update_counter_state();
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	gateway_queue_tx(runtime.last_tx_k, runtime.last_tx_m,
		runtime.last_programmed_tx_timestamp, true, !runtime.synchronized);
#endif
	if (runtime.last_tx_m + 1U < HEIMDALL_M) {
		next_result = build_and_schedule(
			runtime.last_tx_k, runtime.last_tx_m + 1U,
			(runtime.last_programmed_tx_timestamp + SLOT_DTU) &
				DWT_TIMESTAMP_MASK, false);
	}
	if (next_result != 0) {
		if (runtime.last_tx_m + 1U >= HEIMDALL_M) {
			runtime.tx_report_valid = false;
		}
		rearm_rx();
		schedule_after_frame(runtime.last_tx_k, runtime.last_tx_m,
			runtime.last_programmed_tx_timestamp, 0U);
	}
}

static void tx_timeout_handler(struct k_work *work)
{
	struct k_work_delayable *delayable = k_work_delayable_from_work(work);
	struct tx_timeout_context *context = CONTAINER_OF(
		delayable, struct tx_timeout_context, work);
	uint32_t k;
	uint64_t timestamp;
	uint8_t m;
	unsigned int key = irq_lock();

	if (!runtime.tx_armed || context->generation != runtime.tx_generation) {
		irq_unlock(key);
		return;
	}
	dwt_forcetrxoff();
	runtime.tx_armed = false;
	k = runtime.last_tx_k;
	m = runtime.last_tx_m;
	timestamp = runtime.last_programmed_tx_timestamp;
	irq_unlock(key);
	heimdall_runtime_counters.tx_timeout_recoveries++;
	update_counter_state();
	rearm_rx();
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	gateway_queue_tx(k, m, timestamp, false, !runtime.synchronized);
#endif
	schedule_after_frame(k, m, timestamp, TX_COMPLETION_TIMEOUT_US);
}

#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
static void gateway_status_handler(struct k_work *work)
{
	uint8_t sync_state = runtime.synchronized ? 1U :
		(CONFIG_HEIMDALL_NODE_ID == HEIMDALL_MASTER_NODE_ID ? 2U : 0U);

	ARG_UNUSED(work);
	if (gateway_emit_hello_next) {
		struct gateway_rx_event *hello = gateway_event_alloc(
			GATEWAY_EVENT_HELLO, !runtime.synchronized);

		if (hello != NULL) {
			hello->hello_device_id = gateway_device_id;
			gateway_event_submit(hello);
		}
	}
	gateway_emit_hello_next = !gateway_emit_hello_next;
	struct gateway_rx_event *heartbeat = gateway_event_alloc(
		GATEWAY_EVENT_HEARTBEAT, sync_state != 1U);

	if (heartbeat != NULL) {
		heartbeat->heartbeat.cycles_completed =
			heimdall_runtime_counters.tx_completed;
		heartbeat->heartbeat.sync_state = sync_state;
		heartbeat->heartbeat.evidence_age = runtime.evidence_age;
		gateway_event_submit(heartbeat);
	}
	(void)k_work_reschedule(&gateway_status_work, K_MSEC(500));
}

static void cycle_summary_handler(struct k_work *work)
{
	struct heimdall_usb_cycle_summary summary = {0};
	uint32_t validation_rejects = validation_reject_total();
	uint32_t usb_queue_drops;
	uint16_t received;
	struct gateway_rx_event *event;

	ARG_UNUSED(work);
	summary.k_cycle_start = summary_k_cycle_start;
	summary.cycle_index = summary_k_cycle_start / HEIMDALL_N_NODES;
	received = saturating_delta(heimdall_runtime_counters.rx_validated,
				    summary_rx_validated, &summary.flags);
	summary.frames_received = received;
	summary.frames_expected = (HEIMDALL_N_NODES - 1U) * HEIMDALL_M;
	summary.fcs_errors = saturating_delta(
		heimdall_runtime_counters.rx_fcs_errors,
		summary_rx_fcs_errors, &summary.flags);
	summary.filter_rejects = saturating_delta(
		heimdall_runtime_counters.rx_filter_rejects,
		summary_filter_rejects, &summary.flags);
	summary.validation_rejects = saturating_delta(validation_rejects,
		summary_validation_rejects, &summary.flags);
	summary.subreport_crc_failures = saturating_delta(
		heimdall_runtime_counters.subreport_crc_failures,
		summary_subreport_crc_failures, &summary.flags);
	summary.rx_callback_max_us = MIN(cycle_callback_max_us, UINT16_MAX);
	if (cycle_callback_max_us > UINT16_MAX) {
		summary.flags |= BIT(0);
	}
	for (uint8_t node = 0U; node < HEIMDALL_N_NODES; ++node) {
		if (node != CONFIG_HEIMDALL_NODE_ID &&
		    !summary_peer_received[node]) {
			summary.peer_m0_miss[node] = 1U;
		}
	}
	summary.evidence_age = runtime.evidence_age;
	event = gateway_event_alloc(GATEWAY_EVENT_SUMMARY, !runtime.synchronized);
	if (event != NULL) {
		usb_queue_drops = heimdall_usb_drop_count_take();
		summary.usb_queue_drops = saturating_delta(
			usb_queue_drops, 0U, &summary.flags);
		event->summary.value = summary;
		event->summary.drop_ack = usb_queue_drops;
		gateway_event_submit(event);
	}
	summary_rx_validated = heimdall_runtime_counters.rx_validated;
	summary_rx_fcs_errors = heimdall_runtime_counters.rx_fcs_errors;
	summary_filter_rejects = heimdall_runtime_counters.rx_filter_rejects;
	summary_validation_rejects = validation_rejects;
	summary_subreport_crc_failures =
		heimdall_runtime_counters.subreport_crc_failures;
	cycle_callback_max_us = 0U;
	memset(summary_peer_received, 0, sizeof(summary_peer_received));
	summary_cycle_active = false;
}
#endif

static dwt_callbacks_s runtime_callbacks = {
	.cbTxDone = tx_done_cb,
	.cbRxOk = rx_ok_cb,
	.cbRxTo = rx_error_cb,
	.cbRxErr = rx_error_cb,
};

static void bootstrap_handler(struct k_work *work)
{
	uint64_t target;

	ARG_UNUSED(work);
	if (runtime.synchronized || runtime.tx_armed ||
	    CONFIG_HEIMDALL_NODE_ID != HEIMDALL_MASTER_NODE_ID) {
		return;
	}
	target = (read_system_timestamp40() + SLOT_DTU) & DWT_TIMESTAMP_MASK;
	if (build_and_schedule(0U, 0U, target, false) == 0) {
		heimdall_runtime_counters.bootstrap_transmissions++;
	} else {
		(void)k_work_reschedule(&bootstrap_work,
					 K_MSEC(HEIMDALL_SLOT_DURATION_US / 1000U));
	}
}

static void watchdog_handler(struct k_work *work)
{
	uint32_t attempted_k;
	uint8_t attempted_m;
	uint64_t attempted_timestamp;
	int ret;

	ARG_UNUSED(work);
	if (runtime.tx_armed || !runtime.fallback_valid) {
		return;
	}
	attempted_k = runtime.fallback_k;
	attempted_m = runtime.fallback_m;
	attempted_timestamp = runtime.fallback_timestamp;
	ret = build_and_schedule(attempted_k, attempted_m, attempted_timestamp, true);
	if (ret != 0) {
		rearm_rx();
	}
	if (ret == -ETIME) {
		schedule_after_frame(attempted_k, attempted_m, attempted_timestamp, 0U);
	}
}

int heimdall_beacon_runtime_run(void)
{
	uint8_t device_id[8];
	uint32_t device_id_low;
	uint32_t device_id_high;

	if (hwinfo_get_device_id(device_id, sizeof(device_id)) !=
	    sizeof(device_id)) {
		printk("heimdall: device identity read failed\n");
		return -EIO;
	}
	device_id_high = sys_get_be32(&device_id[0]);
	device_id_low = sys_get_be32(&device_id[4]);
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	gateway_device_id = ((uint64_t)device_id_high << 32) | device_id_low;
#endif

	if ((CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_LOW == 0U &&
	     CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_HIGH == 0U) ||
	    device_id_low != CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_LOW ||
	    device_id_high != CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_HIGH) {
		printk("heimdall: identity mismatch node=%u actual=%08x%08x expected=%08x%08x\n",
		       CONFIG_HEIMDALL_NODE_ID, device_id_high, device_id_low,
		       CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_HIGH,
		       CONFIG_HEIMDALL_EXPECTED_DEVICE_ID_LOW);
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0002U, CONFIG_HEIMDALL_NODE_ID, 0U, true);
#endif
		return -EACCES;
	}
	if (CONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU == 0U ||
	    CONFIG_HEIMDALL_RX_ANTENNA_DELAY_DTU == 0U) {
		printk("heimdall: per-board antenna delays are not configured\n");
		return -EINVAL;
	}

	memset(&runtime, 0, sizeof(runtime));
	memset((void *)&heimdall_runtime_counters, 0,
	       sizeof(heimdall_runtime_counters));
	runtime.evidence_age = CONFIG_HEIMDALL_NODE_ID ==
		HEIMDALL_MASTER_NODE_ID ? 0U : UINT8_MAX;
	runtime.evidence_min_received = UINT8_MAX;
	update_counter_state();
	k_work_init_delayable(&bootstrap_work, bootstrap_handler);
	k_work_init_delayable(&watchdog_work, watchdog_handler);
	for (uint8_t m = 0U; m < HEIMDALL_M; ++m) {
		k_work_init_delayable(&tx_timeout_contexts[m].work,
				      tx_timeout_handler);
	}
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	k_work_init_delayable(&gateway_status_work, gateway_status_handler);
	gateway_emit_hello_next = true;
#endif

	dwt_settxantennadelay(CONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU);
	dwt_setrxantennadelay(CONFIG_HEIMDALL_RX_ANTENNA_DELAY_DTU);
	dwt_configciadiag(DW_CIA_DIAG_LOG_ALL);
	dwt_setrxtimeout(0U);
	dwt_setpreambledetecttimeout(0U);
	dwt_enableautoack(0U, 0);
	dwt_setcallbacks(&runtime_callbacks);
	dwt_setinterrupt(DWT_INT_TXFRS_BIT_MASK | DWT_INT_RXFCG_BIT_MASK |
			 SYS_STATUS_ALL_RX_ERR, 0U, DWT_ENABLE_INT);
	if (dw3000_hw_init_interrupt() != 0) {
		printk("heimdall: IRQ init failed\n");
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		gateway_queue_error(0x0003U, 0U, 0U, true);
#endif
		return -EIO;
	}

	printk("heimdall: runtime node=%u master=%u N=%u M=%u slot_us=%u frame=%u device=%08x%08x tx_ant=%u rx_ant=%u\n",
	       CONFIG_HEIMDALL_NODE_ID, HEIMDALL_MASTER_NODE_ID,
	       HEIMDALL_N_NODES, HEIMDALL_M, HEIMDALL_SLOT_DURATION_US,
	       HEIMDALL_FRAME_BYTES, device_id_high, device_id_low,
	       CONFIG_HEIMDALL_TX_ANTENNA_DELAY_DTU,
	       CONFIG_HEIMDALL_RX_ANTENNA_DELAY_DTU);
	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	(void)k_work_reschedule(&gateway_status_work, K_NO_WAIT);
#endif
	if (CONFIG_HEIMDALL_NODE_ID == HEIMDALL_MASTER_NODE_ID) {
		(void)k_work_reschedule(&bootstrap_work,
					 K_MSEC(BOOTSTRAP_LISTEN_MS));
	}

	while (true) {
		k_msleep(1000);
	}
}
