#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
#include "usb_cir_stream.h"
#endif

#include <deca_device_api.h>
#include <dw3000_hw.h>

#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
#include "heimdall_beacon_config.h"
#include "beacon_report.h"
#include "beacon_schedule.h"
#include "beacon_wire.h"
#endif

#define DWT_TIMESTAMP_MASK ((1ULL << 40) - 1ULL)
#define DWT_DTU_PER_MS 63897600ULL
#define TX_ANT_DLY 16385U
#define RX_ANT_DLY 16385U
#define CIR_TAPS CONFIG_PHASE1_CIR_TAPS
#define CIR_LEAD_TAPS CONFIG_PHASE1_CIR_LEFT_TAPS
#define RX_BUFFER_LEN 1023U
#define FRAME_DATA_LEN (CONFIG_PHASE1_TX_FRAME_BYTES - 2U)
#define FRAME_WIRE_LEN CONFIG_PHASE1_TX_FRAME_BYTES
#if defined(CONFIG_PHASE1_ENABLE_FRAME_FILTER)
#define PHASE1_PAYLOAD_OFFSET 9U
#else
#define PHASE1_PAYLOAD_OFFSET 0U
#endif

static uint64_t timestamp40(const uint8_t timestamp[5])
{
	uint64_t value = 0;

	for (int i = 4; i >= 0; i--) {
		value = (value << 8) | timestamp[i];
	}
	return value;
}

static uint64_t system_timestamp40(const uint8_t timestamp[4])
{
	uint64_t value = 0U;

	for (int i = 3; i >= 0; i--) {
		value = (value << 8) | timestamp[i];
	}
	return (value << 8) & DWT_TIMESTAMP_MASK;
}

#if defined(CONFIG_PHASE1_ROLE_SCHEDULED_TX)
static int64_t timestamp40_diff(uint64_t actual, uint64_t expected)
{
	uint64_t delta = (actual - expected) & DWT_TIMESTAMP_MASK;

	if ((delta & (1ULL << 39)) != 0U) {
		return (int64_t)(delta - (1ULL << 40));
	}
	return (int64_t)delta;
}
#endif

#if defined(CONFIG_PHASE1_ROLE_SCHEDULED_TX)
K_SEM_DEFINE(scheduled_tx_done, 0, 1);
static volatile uint32_t scheduled_tx_irq_count;
static volatile uint32_t scheduled_tx_write_max_us;

static void scheduled_tx_confirm_cb(const dwt_cb_data_t *cb_data)
{
	ARG_UNUSED(cb_data);
	scheduled_tx_irq_count++;
	k_sem_give(&scheduled_tx_done);
}

static dwt_callbacks_s scheduled_tx_callbacks = {
	.cbTxDone = scheduled_tx_confirm_cb,
};
#endif

int phase1_run_scheduled_tx(void)
{
#if defined(CONFIG_PHASE1_ROLE_SCHEDULED_TX)
	static uint8_t frame[FRAME_DATA_LEN];
	uint8_t system_timestamp[4];
	uint8_t tx_timestamp[5];
	uint64_t target;
	int64_t error_sum = 0;
	uint64_t error_abs_sum = 0;
	uint64_t error_sq_sum = 0;
	int64_t error_min = INT64_MAX;
	int64_t error_max = INT64_MIN;
	uint32_t start_failures = 0;
	uint32_t irq_timeouts = 0;

	memset(frame, 0xA5, sizeof(frame));
#if defined(CONFIG_PHASE1_ENABLE_FRAME_FILTER)
	frame[0] = 0x41;
	frame[1] = 0x88;
	frame[3] = (uint8_t)CONFIG_PHASE1_NETWORK_ID;
	frame[4] = (uint8_t)(CONFIG_PHASE1_NETWORK_ID >> 8);
	frame[5] = 0xFF;
	frame[6] = 0xFF;
	frame[7] = (uint8_t)CONFIG_PHASE1_NODE_ID;
	frame[PHASE1_PAYLOAD_OFFSET] = 0xC5;
	frame[PHASE1_PAYLOAD_OFFSET + 4U] = 'P';
	frame[PHASE1_PAYLOAD_OFFSET + 5U] = '1';
#else
	frame[0] = 0xC5;
	frame[4] = 'P';
	frame[5] = '1';
#endif

	dwt_settxantennadelay(TX_ANT_DLY);
	dwt_setcallbacks(&scheduled_tx_callbacks);
	dwt_setinterrupt(DWT_INT_TXFRS_BIT_MASK, 0, DWT_ENABLE_INT);
	if (dw3000_hw_init_interrupt() != 0) {
		printk("phase1: SCHED IRQ init failed\n");
		return -EIO;
	}

	printk("phase1: SCHED_START frames=%d period_ms=%d tx_ant_dly=%u mode=IRQ\n",
	       CONFIG_PHASE1_FRAME_COUNT, CONFIG_PHASE1_TX_PERIOD_MS, TX_ANT_DLY);
	dwt_readsystime(system_timestamp);
	target = (system_timestamp40(system_timestamp) +
		  (20ULL * DWT_DTU_PER_MS)) &
		 DWT_TIMESTAMP_MASK;

	for (uint32_t sequence = 0;
	     IS_ENABLED(CONFIG_PHASE1_CONTINUOUS_TX) ||
	     sequence < CONFIG_PHASE1_FRAME_COUNT;
	     sequence++) {
		uint32_t delayed_time = (uint32_t)(target >> 8);
		uint32_t tx_write_start;
		uint32_t tx_write_us;
		uint64_t programmed =
			((((uint64_t)(delayed_time & 0xFFFFFFFEUL)) << 8) +
			 TX_ANT_DLY) & DWT_TIMESTAMP_MASK;
		int64_t error;

#if defined(CONFIG_PHASE1_ENABLE_FRAME_FILTER)
		frame[2] = (uint8_t)sequence;
		frame[PHASE1_PAYLOAD_OFFSET + 2U] = (uint8_t)sequence;
		frame[PHASE1_PAYLOAD_OFFSET + 3U] = (uint8_t)(sequence >> 8);
#else
		frame[1] = (uint8_t)sequence;
		frame[2] = (uint8_t)sequence;
		frame[3] = (uint8_t)(sequence >> 8);
#endif
		tx_write_start = k_cycle_get_32();
		dwt_writetxdata(FRAME_DATA_LEN, frame, 0);
		tx_write_us = k_cyc_to_us_floor32(k_cycle_get_32() - tx_write_start);
		if (tx_write_us > scheduled_tx_write_max_us) {
			scheduled_tx_write_max_us = tx_write_us;
		}
		dwt_writetxfctrl(FRAME_WIRE_LEN, 0, 0);
		dwt_setdelayedtrxtime(delayed_time);

		if (dwt_starttx(DWT_START_TX_DELAYED) == DWT_ERROR) {
			start_failures++;
		} else if (k_sem_take(&scheduled_tx_done,
				      K_MSEC(CONFIG_PHASE1_TX_PERIOD_MS + 100)) != 0) {
			irq_timeouts++;
		} else {
			dwt_readtxtimestamp(tx_timestamp);
			error = timestamp40_diff(timestamp40(tx_timestamp), programmed);
			error_sum += error;
			error_abs_sum += (uint64_t)(error < 0 ? -error : error);
			error_sq_sum += (uint64_t)(error * error);
			if (error < error_min) {
				error_min = error;
			}
			if (error > error_max) {
				error_max = error;
			}
			if (((sequence + 1U) % 100U) == 0U) {
				printk("phase1: SCHED_PROGRESS sent=%u irq=%u tx_write_max_us=%u error_dtu=%lld\n",
				       sequence + 1U, scheduled_tx_irq_count,
				       scheduled_tx_write_max_us, error);
			}
		}

		target = (target +
			  ((uint64_t)CONFIG_PHASE1_TX_PERIOD_MS * DWT_DTU_PER_MS)) &
			 DWT_TIMESTAMP_MASK;
	}

	uint32_t completed = scheduled_tx_irq_count;
	printk("phase1: SCHED_FINAL sent=%d irq=%u start_fail=%u irq_to=%u tx_write_max_us=%u error_min_dtu=%lld error_max_dtu=%lld error_mean_dtu=%lld error_abs_mean_dtu=%llu error_sq_sum=%llu\n",
	       CONFIG_PHASE1_FRAME_COUNT, completed, start_failures, irq_timeouts,
	       scheduled_tx_write_max_us,
	       completed ? error_min : 0, completed ? error_max : 0,
	       completed ? error_sum / (int64_t)completed : 0,
	       completed ? error_abs_sum / completed : 0,
	       completed ? error_sq_sum : 0);
	return 0;
#else
	return -ENOTSUP;
#endif
}

#if defined(CONFIG_PHASE1_ROLE_SENSING_RX)
static uint8_t sensing_rx_buffer[RX_BUFFER_LEN];
static uint8_t cir_raw[CIR_TAPS * 6U];
#if defined(CONFIG_PHASE1_CIR_DUMP)
static char cir_line[1400];
static int16_t cir_full[2U * DWT_CIR_LEN_IP_PRF64];
#endif
static volatile uint32_t sensing_rx_count;
static volatile uint32_t sensing_rx_errors;
static volatile uint32_t sensing_cir_reads;
static volatile uint32_t sensing_last_uptime_ms;
static volatile int64_t sensing_cfo_raw_sum;
static volatile int16_t sensing_cfo_raw_min = INT16_MAX;
static volatile int16_t sensing_cfo_raw_max = INT16_MIN;
static volatile int16_t sensing_cfo_raw_last;
static volatile uint64_t sensing_rx_timestamp_last;
static volatile uint16_t sensing_fp_last;
static volatile uint16_t sensing_start_last;
static volatile uint8_t sensing_peak_last;
static volatile uint64_t sensing_lead_power_last;
static volatile uint64_t sensing_peak_power_last;
static volatile uint64_t sensing_tail_power_last;
#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
static const struct gpio_dt_spec sensing_measure_pin =
	GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static volatile uint32_t sensing_callback_max_us;
static volatile uint32_t sensing_diag_max_us;
static volatile uint32_t sensing_cir_max_us;
#endif
#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
BUILD_ASSERT(CIR_TAPS == HEIMDALL_CIR_TAPS);
BUILD_ASSERT(CIR_LEAD_TAPS == HEIMDALL_CIR_LEFT_TAPS);
BUILD_ASSERT(HEIMDALL_FRAME_HEADER_BYTES + HEIMDALL_FRAME_PAYLOAD_BYTES ==
	     HEIMDALL_FRAME_BYTES - 2U);
static int16_t sensing_cir_iq[2U * HEIMDALL_CIR_TAPS];
static uint8_t sensing_subreports[HEIMDALL_N_NODES][HEIMDALL_SUBREPORT_BYTES];
static const uint8_t *sensing_subreport_ptrs[HEIMDALL_REPORT_NODE_SLOTS];
static uint16_t sensing_subreport_lengths[HEIMDALL_REPORT_NODE_SLOTS];
static struct heimdall_report sensing_report;
static uint8_t sensing_report_frames[HEIMDALL_M][HEIMDALL_FRAME_BYTES - 2U];
static volatile uint32_t sensing_subreport_encode_max_us;
static volatile uint32_t sensing_crc_only_max_us;
static volatile uint32_t sensing_report_assembly_max_us;
static volatile uint32_t sensing_report_assembly_failures;
static volatile uint8_t sensing_report_frame_sink;
static volatile uint32_t sensing_crc_sink;
#endif

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

#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
static void sensing_update_max_us(volatile uint32_t *maximum, uint32_t start)
{
	uint32_t elapsed_us = k_cyc_to_us_floor32(k_cycle_get_32() - start);

	if (elapsed_us > *maximum) {
		*maximum = elapsed_us;
	}
}
#endif

#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
static int sensing_assemble_report(uint32_t k, uint8_t observed_node,
				  uint64_t rx_timestamp, int16_t cfo_raw,
				  uint16_t cir_start, uint8_t dgc_decision,
				  const dwt_cirdiags_t *diag)
{
	struct heimdall_subreport subreport = {
		.observed_node_id = observed_node,
		.obs_flags = BIT(0) | BIT(2),
		.observed_m = 0U,
		.round_delta = heimdall_schedule_round_delta(
			CONFIG_HEIMDALL_NODE_ID, observed_node, HEIMDALL_N_NODES),
		.observed_tx_timestamp = 0U,
		.rx_timestamp = rx_timestamp,
		.cfo_raw = cfo_raw,
		.fp_index_q10_6 = diag->FpIndex,
		.f1 = diag->F1,
		.f2 = diag->F2,
		.f3 = diag->F3,
		.ip_power = diag->power,
		.accum_count = diag->accumCount,
		.dgc_decision = dgc_decision,
		.cir_start_offset = cir_start,
		.cir_taps = HEIMDALL_CIR_TAPS,
		.cir_iq = sensing_cir_iq,
	};
	uint32_t operation_start;
	int ret;

	if (observed_node >= HEIMDALL_N_NODES ||
	    observed_node == CONFIG_HEIMDALL_NODE_ID) {
		return -EINVAL;
	}
	if (cir_start == 0U && ((diag->FpIndex + 32U) >> 6) < CIR_LEAD_TAPS) {
		subreport.obs_flags |= BIT(1);
	}

	operation_start = k_cycle_get_32();
	ret = heimdall_subreport_encode(&subreport,
					 sensing_subreports[observed_node],
					 sizeof(sensing_subreports[observed_node]));
	sensing_update_max_us(&sensing_subreport_encode_max_us, operation_start);
	if (ret != 0) {
		return ret;
	}
	sensing_subreport_ptrs[observed_node] = sensing_subreports[observed_node];
	sensing_subreport_lengths[observed_node] = HEIMDALL_SUBREPORT_BYTES;

	operation_start = k_cycle_get_32();
	ret = heimdall_report_pack(&sensing_report, k, CONFIG_HEIMDALL_NODE_ID,
				   HEIMDALL_N_NODES, sensing_subreport_ptrs,
				   sensing_subreport_lengths);
	for (uint8_t m = 0U; ret == 0 && m < HEIMDALL_M; ++m) {
		struct heimdall_frame_header header = {
			.mac_seq = (uint8_t)(k * HEIMDALL_M + m),
			.network_id = HEIMDALL_NETWORK_ID,
			.src_addr = CONFIG_HEIMDALL_NODE_ID,
			.protocol_version = HEIMDALL_PROTOCOL_VERSION,
			.frame_type = 0U,
			.m = m,
			.k = k,
			.n_nodes = HEIMDALL_N_NODES,
			.slots_per_superslot = HEIMDALL_M,
			.config_hash = HEIMDALL_CONFIG_HASH,
			.tx_timestamp = 0U,
			.subreport_count = heimdall_report_frame_subreport_count(
				&sensing_report, m, HEIMDALL_FRAME_PAYLOAD_BYTES),
			.pooled_total_bytes = sensing_report.total_bytes,
			.peer_observed_bitmap = sensing_report.peer_observed_bitmap,
			.evidence_age = 0U,
			.flags = 0U,
		};

		ret = heimdall_frame_header_encode(
			&header, sensing_report_frames[m], sizeof(sensing_report_frames[m]));
		if (ret == 0) {
			ret = heimdall_report_copy_frame(
				&sensing_report, m, HEIMDALL_FRAME_PAYLOAD_BYTES,
				&sensing_report_frames[m][HEIMDALL_FRAME_HEADER_BYTES],
				HEIMDALL_FRAME_PAYLOAD_BYTES);
		}
	}
	sensing_update_max_us(&sensing_report_assembly_max_us, operation_start);
	if (ret == 0) {
		sensing_report_frame_sink =
			sensing_report_frames[HEIMDALL_M - 1U][HEIMDALL_FRAME_BYTES - 3U];
	}
	return ret;
}

static void sensing_measure_crc32(void)
{
	uint32_t crc = 0U;

	for (uint16_t i = 0U; i < 256U; ++i) {
		uint32_t operation_start;

		sensing_subreports[0][0] = (uint8_t)i;
		operation_start = k_cycle_get_32();
		crc ^= heimdall_crc32(sensing_subreports[0], HEIMDALL_SUBREPORT_BYTES - 4U);
		sensing_update_max_us(&sensing_crc_only_max_us, operation_start);
	}
	sensing_crc_sink = crc;
}
#endif

static void sensing_rx_ok_cb(const dwt_cb_data_t *cb_data)
{
	uint32_t callback_start = 0U;
	uint32_t operation_start = 0U;
	dwt_cirdiags_t diag;
	uint8_t rx_timestamp[5];
	uint16_t length = cb_data->datalength;
	uint16_t sequence;
	uint16_t fp_index;
	uint8_t agc_state;
	int16_t rssi_q8_8;
	int16_t fp_power_q8_8;
	uint16_t start;
	int16_t cfo_raw;
	uint64_t lead_power = 0;
	uint64_t peak_power = 0;
	uint64_t tail_power = 0;
	uint8_t peak_index = 0;
#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	callback_start = k_cycle_get_32();
	(void)gpio_pin_set_dt(&sensing_measure_pin, 1);
#endif
#if defined(CONFIG_PHASE1_CIR_DUMP) && !defined(CONFIG_USB_DEVICE_STACK_NEXT)
	uint16_t full_peak_index = 0;
#endif

	if (length > sizeof(sensing_rx_buffer)) {
		length = sizeof(sensing_rx_buffer);
	}
	dwt_readrxdata(sensing_rx_buffer, length, 0);
	if ((length < PHASE1_PAYLOAD_OFFSET + 6U) ||
	    (sensing_rx_buffer[PHASE1_PAYLOAD_OFFSET] != 0xC5) ||
	    (sensing_rx_buffer[PHASE1_PAYLOAD_OFFSET + 4U] != 'P') ||
	    (sensing_rx_buffer[PHASE1_PAYLOAD_OFFSET + 5U] != '1')) {
		(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
		#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
		(void)gpio_pin_set_dt(&sensing_measure_pin, 0);
		#endif
		return;
	}

	sequence = (uint16_t)sensing_rx_buffer[PHASE1_PAYLOAD_OFFSET + 2U] |
		   ((uint16_t)sensing_rx_buffer[PHASE1_PAYLOAD_OFFSET + 3U] << 8);
	dwt_readrxtimestamp(rx_timestamp, DWT_IP_M);
	cfo_raw = dwt_readclockoffset();
	#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	operation_start = k_cycle_get_32();
	#endif
	if (dwt_readdiagnostics_acc(&diag, DWT_ACC_IDX_IP_M) != DWT_SUCCESS) {
		sensing_rx_errors++;
		(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
		#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
		(void)gpio_pin_set_dt(&sensing_measure_pin, 0);
		#endif
		return;
	}
	#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	sensing_update_max_us(&sensing_diag_max_us, operation_start);
	#endif
	agc_state = dwt_get_dgcdecision();
	rssi_q8_8 = 0;
	fp_power_q8_8 = 0;
	(void)dwt_calculate_rssi(&diag, DWT_ACC_IDX_IP_M, &rssi_q8_8);
	(void)dwt_calculate_first_path_power(&diag, DWT_ACC_IDX_IP_M,
					     &fp_power_q8_8);

	fp_index = (uint16_t)((diag.FpIndex + 32U) >> 6);
	start = fp_index > CIR_LEAD_TAPS ? fp_index - CIR_LEAD_TAPS : 0U;
	#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	operation_start = k_cycle_get_32();
	#endif
	dwt_readcir_48b(cir_raw, DWT_ACC_IDX_IP_M, start, CIR_TAPS);
	#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	sensing_update_max_us(&sensing_cir_max_us, operation_start);
	#endif
	sensing_cir_reads++;

#if defined(CONFIG_PHASE1_CIR_DUMP) && !defined(CONFIG_USB_DEVICE_STACK_NEXT)
	dwt_readcir((uint32_t *)cir_full, DWT_ACC_IDX_IP_M, 0,
		    DWT_CIR_LEN_IP_PRF64, DWT_CIR_READ_HI);
	uint64_t full_peak_power = 0;
	for (uint16_t i = 0; i < DWT_CIR_LEN_IP_PRF64; i++) {
		int32_t real = cir_full[2U * i];
		int32_t imag = cir_full[2U * i + 1U];
		uint64_t power = (uint64_t)((int64_t)real * real) +
				 (uint64_t)((int64_t)imag * imag);
		if (power > full_peak_power) {
			full_peak_power = power;
			full_peak_index = i;
		}
	}
#endif

	for (uint8_t i = 0; i < CIR_TAPS; i++) {
		const uint8_t *sample = &cir_raw[i * 6U];
		int32_t real = sign_extend_18(sample);
		int32_t imag = sign_extend_18(sample + 3);
		uint64_t power = (uint64_t)((int64_t)real * real) +
				 (uint64_t)((int64_t)imag * imag);

#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
		sensing_cir_iq[2U * i] = (int16_t)(real >> 2);
		sensing_cir_iq[2U * i + 1U] = (int16_t)(imag >> 2);
#endif

		if (i < 8U) {
			lead_power += power;
		}
		if (i >= 40U) {
			tail_power += power;
		}
		if (power > peak_power) {
			peak_power = power;
			peak_index = i;
		}
	}

	sensing_rx_count++;
	sensing_last_uptime_ms = k_uptime_get_32();
	sensing_cfo_raw_last = cfo_raw;
	sensing_cfo_raw_sum += cfo_raw;
	if (cfo_raw < sensing_cfo_raw_min) {
		sensing_cfo_raw_min = cfo_raw;
	}
	if (cfo_raw > sensing_cfo_raw_max) {
		sensing_cfo_raw_max = cfo_raw;
	}
	sensing_rx_timestamp_last = timestamp40(rx_timestamp);
	sensing_fp_last = fp_index;
	sensing_start_last = start;
	sensing_peak_last = peak_index;
	sensing_lead_power_last = lead_power / 8U;
	sensing_peak_power_last = peak_power;
	sensing_tail_power_last = tail_power / 24U;

#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
	if (sensing_assemble_report(sequence,
#if defined(CONFIG_PHASE1_ENABLE_FRAME_FILTER)
				    sensing_rx_buffer[7],
#else
				    heimdall_schedule_transmitter(sequence, HEIMDALL_N_NODES),
#endif
				    sensing_rx_timestamp_last, cfo_raw, start, agc_state,
				    &diag) != 0) {
		sensing_report_assembly_failures++;
	}
#endif

	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);

#if defined(CONFIG_PHASE1_CIR_DUMP)
#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
	live_cir_stream_enqueue_binary(sequence, sensing_rx_timestamp_last, cfo_raw,
				       fp_index, agc_state, rssi_q8_8,
				       fp_power_q8_8, cir_raw);
#else
	int line_length = snprintk(cir_line, sizeof(cir_line),
		"CIR,seq=%u,rx_ts=%llu,cfo_raw=%d,fp=%u,fp_raw=%u,diag_peak=%u,full_peak=%u,start=%u,taps=",
		sequence, sensing_rx_timestamp_last, cfo_raw, fp_index,
		diag.FpIndex, diag.peakIndex, full_peak_index, start);
	for (uint8_t i = 0; i < CIR_TAPS; i++) {
		const uint8_t *sample = &cir_raw[i * 6U];
		line_length += snprintk(cir_line + line_length,
					 sizeof(cir_line) - line_length,
					 "%s%d:%d", i == 0U ? "" : ";",
					 sign_extend_18(sample) >> 2,
					 sign_extend_18(sample + 3) >> 2);
	}
	printk("%s\n", cir_line);
#endif
#endif

#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	uint32_t elapsed_us = k_cyc_to_us_floor32(k_cycle_get_32() - callback_start);
	if (elapsed_us > sensing_callback_max_us) {
		sensing_callback_max_us = elapsed_us;
	}
	(void)gpio_pin_set_dt(&sensing_measure_pin, 0);
#endif
}

static void sensing_rx_error_cb(const dwt_cb_data_t *cb_data)
{
	ARG_UNUSED(cb_data);
	sensing_rx_errors++;
	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
}

static dwt_callbacks_s sensing_rx_callbacks = {
	.cbRxOk = sensing_rx_ok_cb,
	.cbRxTo = sensing_rx_error_cb,
	.cbRxErr = sensing_rx_error_cb,
};
#endif

int phase1_run_sensing_rx(void)
{
#if defined(CONFIG_PHASE1_ROLE_SENSING_RX)
	uint32_t last_printed = 0;
	bool final_printed = false;

#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
	if (!gpio_is_ready_dt(&sensing_measure_pin) ||
	    gpio_pin_configure_dt(&sensing_measure_pin, GPIO_OUTPUT_INACTIVE) != 0) {
		printk("phase1: callback measurement GPIO unavailable\n");
		return -ENODEV;
	}
#endif

#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
	sensing_measure_crc32();
#endif

	dwt_setrxantennadelay(RX_ANT_DLY);
	dwt_configciadiag(DW_CIA_DIAG_LOG_ALL);
	dwt_setcallbacks(&sensing_rx_callbacks);
	dwt_setinterrupt(DWT_INT_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_ERR,
			 0, DWT_ENABLE_INT);
	if (dw3000_hw_init_interrupt() != 0) {
		printk("phase1: SENSE IRQ init failed\n");
		return -EIO;
	}

	printk("phase1: SENSE_START expected=%d cir_taps=%u lead=%u dump=%d measure=%d assembly=%d mode=IRQ\n",
	       CONFIG_PHASE1_FRAME_COUNT, CIR_TAPS, CIR_LEAD_TAPS,
	       IS_ENABLED(CONFIG_PHASE1_CIR_DUMP),
	       IS_ENABLED(CONFIG_PHASE1_MEASURE_CALLBACK),
	       IS_ENABLED(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY));
	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);

	while (true) {
		uint32_t received = sensing_rx_count;

		if ((received != last_printed) && ((received % 100U) == 0U)) {
			printk("phase1: SENSE_PROGRESS received=%u cir_reads=%u errors=%u rx_ts=%llu cfo_raw=%d fp=%u peak_rel=%u lead_pwr=%llu peak_pwr=%llu tail_pwr=%llu callback_max_us=%u diag_max_us=%u cir_max_us=%u subreport_encode_max_us=%u crc_only_max_us=%u report_assembly_max_us=%u report_assembly_failures=%u\n",
			       received, sensing_cir_reads, sensing_rx_errors,
			       sensing_rx_timestamp_last, sensing_cfo_raw_last,
			       sensing_fp_last, sensing_peak_last,
			       sensing_lead_power_last, sensing_peak_power_last,
			       sensing_tail_power_last,
#if defined(CONFIG_PHASE1_MEASURE_CALLBACK)
			       sensing_callback_max_us, sensing_diag_max_us,
			       sensing_cir_max_us,
#if defined(CONFIG_PHASE1_MEASURE_REPORT_ASSEMBLY)
			       sensing_subreport_encode_max_us,
			       sensing_crc_only_max_us,
			       sensing_report_assembly_max_us,
			       sensing_report_assembly_failures);
#else
			       0U, 0U, 0U, 0U);
#endif
#else
			       0U, 0U, 0U, 0U, 0U, 0U, 0U);
#endif
			last_printed = received;
		}

		if ((received > 0U) && !final_printed &&
		    ((uint32_t)(k_uptime_get_32() - sensing_last_uptime_ms) > 1000U)) {
			int64_t cfo_avg = sensing_cfo_raw_sum / (int64_t)received;
			int64_t cfo_avg_x100_ppm =
				(cfo_avg * 100000000LL) / (1LL << 26);
			int64_t cfo_min_x100_ppm =
				((int64_t)sensing_cfo_raw_min * 100000000LL) /
				(1LL << 26);
			int64_t cfo_max_x100_ppm =
				((int64_t)sensing_cfo_raw_max * 100000000LL) /
				(1LL << 26);
			uint32_t lost = CONFIG_PHASE1_FRAME_COUNT > received ?
				CONFIG_PHASE1_FRAME_COUNT - received : 0U;

			printk("phase1: SENSE_FINAL received=%u expected=%d lost=%u errors=%u cir_reads=%u rx_ts_last=%llu cfo_raw_min=%d cfo_raw_max=%d cfo_raw_avg=%lld cfo_x100ppm_min=%lld cfo_x100ppm_max=%lld cfo_x100ppm_avg=%lld fp=%u start=%u peak_rel=%u lead_pwr=%llu peak_pwr=%llu tail_pwr=%llu\n",
			       received, CONFIG_PHASE1_FRAME_COUNT, lost,
			       sensing_rx_errors, sensing_cir_reads,
			       sensing_rx_timestamp_last, sensing_cfo_raw_min,
			       sensing_cfo_raw_max, cfo_avg, cfo_min_x100_ppm,
			       cfo_max_x100_ppm, cfo_avg_x100_ppm,
			       sensing_fp_last, sensing_start_last,
			       sensing_peak_last, sensing_lead_power_last,
			       sensing_peak_power_last, sensing_tail_power_last);
			final_printed = true;
		}
		if ((received > 0U) &&
		    ((uint32_t)(k_uptime_get_32() - sensing_last_uptime_ms) < 100U)) {
			final_printed = false;
		}
		k_msleep(20);
	}
#else
	return -ENOTSUP;
#endif
}
