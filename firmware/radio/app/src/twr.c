#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include <deca_device_api.h>

#define TX_ANT_DLY 16385U
#define RX_ANT_DLY 16385U
#define UUS_TO_DWT_TIME 65536ULL
#define RESPONDER_DELAY_UUS 3000U
#define SPEED_OF_LIGHT_M_S 299702547.0
#define DWT_DTU_TO_MM (SPEED_OF_LIGHT_M_S * 1000.0 * DWT_TIME_UNITS)
#define COMMON_LEN 10U
#define SEQUENCE_INDEX 2U
#define POLL_RX_TS_INDEX 10U
#define RESP_TX_TS_INDEX 14U

static uint8_t poll_frame[12] = {
	0x41, 0x88, 0, 0xCA, 0xDE, 'W', 'A', 'V', 'E', 0xE0, 0, 0
};
static uint8_t response_frame[20] = {
	0x41, 0x88, 0, 0xCA, 0xDE, 'V', 'E', 'W', 'A', 0xE1,
	0, 0, 0, 0, 0, 0, 0, 0, 0, 0
};

static uint64_t timestamp40(const uint8_t timestamp[5])
{
	uint64_t value = 0;

	for (int i = 4; i >= 0; i--) {
		value = (value << 8) | timestamp[i];
	}
	return value;
}

static uint64_t read_rx_timestamp(void)
{
	uint8_t timestamp[5];

	dwt_readrxtimestamp(timestamp, DWT_IP_M);
	return timestamp40(timestamp);
}

#if defined(CONFIG_PHASE1_ROLE_TWR_INITIATOR)
static uint64_t read_tx_timestamp(void)
{
	uint8_t timestamp[5];

	dwt_readtxtimestamp(timestamp);
	return timestamp40(timestamp);
}
#endif

#if defined(CONFIG_PHASE1_ROLE_TWR_RESPONDER)
static void put_timestamp32(uint8_t *destination, uint64_t timestamp)
{
	for (uint8_t i = 0; i < 4U; i++) {
		destination[i] = (uint8_t)timestamp;
		timestamp >>= 8;
	}
}
#endif

#if defined(CONFIG_PHASE1_ROLE_TWR_INITIATOR)
static uint32_t get_timestamp32(const uint8_t *source)
{
	uint32_t timestamp = 0;

	for (int i = 3; i >= 0; i--) {
		timestamp = (timestamp << 8) | source[i];
	}
	return timestamp;
}
#endif

static uint32_t wait_for_status(uint32_t mask, uint32_t timeout_ms)
{
	uint32_t start = k_uptime_get_32();

	while ((uint32_t)(k_uptime_get_32() - start) < timeout_ms) {
		uint32_t status = dwt_readsysstatuslo();
		if ((status & mask) != 0U) {
			return status;
		}
		k_yield();
	}
	return 0;
}

static bool common_frame_matches(uint8_t *received, const uint8_t *expected)
{
	uint8_t sequence = received[SEQUENCE_INDEX];
	bool matches;

	received[SEQUENCE_INDEX] = 0;
	matches = memcmp(received, expected, COMMON_LEN) == 0;
	received[SEQUENCE_INDEX] = sequence;
	return matches;
}

int phase1_run_twr_initiator(void)
{
#if defined(CONFIG_PHASE1_ROLE_TWR_INITIATOR)
	uint8_t received[sizeof(response_frame)];
	uint32_t successes = 0;
	uint32_t timeouts = 0;
	uint32_t tx_start_failures = 0;
	uint32_t tx_status_timeouts = 0;
	uint32_t rx_status_failures = 0;
	uint32_t bad_responses = 0;
	int64_t raw_mm_sum = 0;
	int64_t corrected_mm_sum = 0;
	int32_t corrected_mm_min = INT32_MAX;
	int32_t corrected_mm_max = INT32_MIN;
	int16_t cfo_raw_min = INT16_MAX;
	int16_t cfo_raw_max = INT16_MIN;
	int64_t cfo_raw_sum = 0;

	dwt_settxantennadelay(TX_ANT_DLY);
	dwt_setrxantennadelay(RX_ANT_DLY);
	printk("phase1: TWR_INIT_START exchanges=%d responder_delay_uus=%u tx_ant_dly=%u rx_ant_dly=%u\n",
	       CONFIG_PHASE1_FRAME_COUNT, RESPONDER_DELAY_UUS,
	       TX_ANT_DLY, RX_ANT_DLY);

	for (uint32_t sequence = 0; sequence < CONFIG_PHASE1_FRAME_COUNT;
	     sequence++) {
		uint32_t status;
		uint32_t poll_tx_ts;
		uint32_t response_rx_ts;
		uint32_t poll_rx_ts;
		uint32_t response_tx_ts;
		int32_t round_trip_initiator;
		int32_t reply_delay_responder;
		int16_t cfo_raw;
		double cfo_ratio;
		double raw_tof_dtu;
		double corrected_tof_dtu;
		int32_t raw_mm;
		int32_t corrected_mm;

		dwt_forcetrxoff();
		poll_frame[SEQUENCE_INDEX] = (uint8_t)sequence;
		dwt_writesysstatuslo(DWT_INT_TXFRS_BIT_MASK |
				     DWT_INT_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_ERR);
		dwt_writetxdata(sizeof(poll_frame) - 2U, poll_frame, 0);
		dwt_writetxfctrl(sizeof(poll_frame), 0, 1);
		if (dwt_starttx(DWT_START_TX_IMMEDIATE) == DWT_ERROR) {
			timeouts++;
			tx_start_failures++;
			k_msleep(20);
			continue;
		}

		status = wait_for_status(DWT_INT_TXFRS_BIT_MASK, 10);
		if (status == 0U) {
			timeouts++;
			tx_status_timeouts++;
			continue;
		}
		poll_tx_ts = (uint32_t)read_tx_timestamp();
		dwt_writesysstatuslo(DWT_INT_TXFRS_BIT_MASK);
		(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);

		status = wait_for_status(DWT_INT_RXFCG_BIT_MASK |
				 SYS_STATUS_ALL_RX_ERR, 10);
		if ((status & DWT_INT_RXFCG_BIT_MASK) == 0U) {
			timeouts++;
			rx_status_failures++;
			dwt_forcetrxoff();
			dwt_writesysstatuslo(SYS_STATUS_ALL_RX_ERR);
			k_msleep(20);
			continue;
		}

		dwt_writesysstatuslo(DWT_INT_RXFCG_BIT_MASK);
		dwt_readrxdata(received, sizeof(received), 0);
		if (!common_frame_matches(received, response_frame)) {
			timeouts++;
			bad_responses++;
			continue;
		}

		response_rx_ts = (uint32_t)read_rx_timestamp();
		cfo_raw = dwt_readclockoffset();
		poll_rx_ts = get_timestamp32(&received[POLL_RX_TS_INDEX]);
		response_tx_ts = get_timestamp32(&received[RESP_TX_TS_INDEX]);
		round_trip_initiator = (int32_t)(response_rx_ts - poll_tx_ts);
		reply_delay_responder = (int32_t)(response_tx_ts - poll_rx_ts);
		cfo_ratio = (double)cfo_raw / (double)(1ULL << 26);
		raw_tof_dtu = ((double)round_trip_initiator -
				(double)reply_delay_responder) / 2.0;
		corrected_tof_dtu = ((double)round_trip_initiator -
			(double)reply_delay_responder * (1.0 - cfo_ratio)) /
			2.0;
		raw_mm = (int32_t)(raw_tof_dtu * DWT_DTU_TO_MM);
		corrected_mm = (int32_t)(corrected_tof_dtu * DWT_DTU_TO_MM);

		successes++;
		raw_mm_sum += raw_mm;
		corrected_mm_sum += corrected_mm;
		cfo_raw_sum += cfo_raw;
		if (corrected_mm < corrected_mm_min) {
			corrected_mm_min = corrected_mm;
		}
		if (corrected_mm > corrected_mm_max) {
			corrected_mm_max = corrected_mm;
		}
		if (cfo_raw < cfo_raw_min) {
			cfo_raw_min = cfo_raw;
		}
		if (cfo_raw > cfo_raw_max) {
			cfo_raw_max = cfo_raw;
		}

		printk("phase1: TWR seq=%u poll_tx=%u resp_rx=%u poll_rx=%u resp_tx=%u cfo_raw=%d raw_mm=%d corrected_mm=%d\n",
		       sequence, poll_tx_ts, response_rx_ts, poll_rx_ts,
		       response_tx_ts, cfo_raw, raw_mm, corrected_mm);
		k_msleep(50);
	}

	printk("phase1: TWR_FINAL requested=%d success=%u timeouts=%u tx_start_fail=%u tx_status_to=%u rx_status_fail=%u bad_resp=%u raw_mm_avg=%lld corrected_mm_avg=%lld corrected_mm_min=%d corrected_mm_max=%d cfo_raw_min=%d cfo_raw_max=%d cfo_raw_avg=%lld\n",
	       CONFIG_PHASE1_FRAME_COUNT, successes, timeouts,
	       tx_start_failures, tx_status_timeouts, rx_status_failures,
	       bad_responses,
	       successes ? raw_mm_sum / successes : 0,
	       successes ? corrected_mm_sum / successes : 0,
	       successes ? corrected_mm_min : 0,
	       successes ? corrected_mm_max : 0,
	       successes ? cfo_raw_min : 0, successes ? cfo_raw_max : 0,
	       successes ? cfo_raw_sum / successes : 0);
	return successes > 0U ? 0 : -EIO;
#else
	return -ENOTSUP;
#endif
}

int phase1_run_twr_responder(void)
{
#if defined(CONFIG_PHASE1_ROLE_TWR_RESPONDER)
	uint8_t received[sizeof(poll_frame)];
	uint32_t responses = 0;
	uint32_t late = 0;
	uint32_t rx_good = 0;
	uint32_t rx_errors = 0;
	uint32_t bad_polls = 0;
	uint32_t idle_timeouts = 0;

	dwt_settxantennadelay(TX_ANT_DLY);
	dwt_setrxantennadelay(RX_ANT_DLY);
	printk("phase1: TWR_RESP_START responder_delay_uus=%u tx_ant_dly=%u rx_ant_dly=%u\n",
	       RESPONDER_DELAY_UUS, TX_ANT_DLY, RX_ANT_DLY);

	while (true) {
		uint32_t status;
		uint64_t poll_rx_ts;
		uint64_t response_tx_ts;
		uint32_t delayed_time;

		dwt_writesysstatuslo(DWT_INT_RXFCG_BIT_MASK |
				     SYS_STATUS_ALL_RX_ERR);
		int32_t rx_enable_result = dwt_rxenable(DWT_START_RX_IMMEDIATE);
		if (rx_enable_result == DWT_ERROR) {
			printk("phase1: TWR_RESP_RX_ENABLE_ERROR\n");
		}
		status = wait_for_status(DWT_INT_RXFCG_BIT_MASK |
				 SYS_STATUS_ALL_RX_ERR, 1000);
		if ((status & DWT_INT_RXFCG_BIT_MASK) == 0U) {
			if (status == 0U) {
				idle_timeouts++;
				if (idle_timeouts <= 3U) {
					printk("phase1: TWR_RESP_IDLE_TIMEOUT count=%u\n",
					       idle_timeouts);
				}
			}
			if (status != 0U) {
				rx_errors++;
				if (rx_errors <= 3U) {
					printk("phase1: TWR_RESP_RX_ERROR status=0x%08x count=%u\n",
					       status, rx_errors);
				}
			}
			dwt_writesysstatuslo(SYS_STATUS_ALL_RX_ERR);
			continue;
		}

		rx_good++;
		dwt_writesysstatuslo(DWT_INT_RXFCG_BIT_MASK);
		dwt_readrxdata(received, sizeof(received), 0);
		if (!common_frame_matches(received, poll_frame)) {
			bad_polls++;
			if (bad_polls <= 3U) {
				printk("phase1: TWR_RESP_BAD_POLL count=%u bytes=%02x,%02x,%02x,%02x len=%u\n",
				       bad_polls, received[0], received[1], received[2],
				       received[9], sizeof(received));
			}
			continue;
		}

		poll_rx_ts = read_rx_timestamp();
		delayed_time =
			(uint32_t)((poll_rx_ts +
			 ((uint64_t)RESPONDER_DELAY_UUS * UUS_TO_DWT_TIME)) >> 8);
		dwt_setdelayedtrxtime(delayed_time);
		response_tx_ts =
			(((uint64_t)(delayed_time & 0xFFFFFFFEUL)) << 8) +
			TX_ANT_DLY;
		put_timestamp32(&response_frame[POLL_RX_TS_INDEX], poll_rx_ts);
		put_timestamp32(&response_frame[RESP_TX_TS_INDEX], response_tx_ts);
		response_frame[SEQUENCE_INDEX] = received[SEQUENCE_INDEX];
		dwt_writetxdata(sizeof(response_frame) - 2U, response_frame, 0);
		dwt_writetxfctrl(sizeof(response_frame), 0, 1);
		if (dwt_starttx(DWT_START_TX_DELAYED) == DWT_ERROR) {
			late++;
			continue;
		}
		status = wait_for_status(DWT_INT_TXFRS_BIT_MASK, 10);
		if ((status & DWT_INT_TXFRS_BIT_MASK) == 0U) {
			late++;
			continue;
		}
		dwt_writesysstatuslo(DWT_INT_TXFRS_BIT_MASK);
		responses++;
		if ((responses % 10U) == 0U) {
			printk("phase1: TWR_RESP_PROGRESS responses=%u late=%u rx_good=%u rx_errors=%u bad_polls=%u\n",
			       responses, late, rx_good, rx_errors, bad_polls);
		}
	}
#else
	return -ENOTSUP;
#endif
}
