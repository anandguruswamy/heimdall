#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
#include <sample_usbd.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/usb/usbd.h>
#endif

#include <deca_device_api.h>
#include <deca_probe_interface.h>
#include <dw3000_hw.h>
#include <dw3000_spi.h>

#if defined(CONFIG_HEIMDALL_BEACON)
#include "heimdall_beacon_config.h"
#include "beacon_wire.h"
#endif

#define HEARTBEAT_PERIOD_MS 500
#define FRAME_COUNT 1000U
#define FRAME_DATA_LEN 10U
#define FRAME_FCS_LEN 2U
#define FRAME_WIRE_LEN (FRAME_DATA_LEN + FRAME_FCS_LEN)
#define TX_PERIOD_MS 20
#define RX_BUFFER_LEN 127U
#define DW3000_NODE DT_COMPAT_GET_ANY_STATUS_OKAY(decawave_dw3000)

int phase1_run_scheduled_tx(void);
int phase1_run_sensing_rx(void);
int phase1_run_twr_initiator(void);
int phase1_run_twr_responder(void);

#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
static const struct device *const usb_uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);

static void usb_msg_cb(struct usbd_context *const ctx, const struct usbd_msg *msg)
{
	if (usbd_can_detect_vbus(ctx)) {
		if (msg->type == USBD_MSG_VBUS_READY) {
			(void)usbd_enable(ctx);
		} else if (msg->type == USBD_MSG_VBUS_REMOVED) {
			(void)usbd_disable(ctx);
		}
	}
}

static void usb_init(void)
{
	struct usbd_context *ctx;

	if (!device_is_ready(usb_uart)) {
		printk("USB CDC device not ready\\n");
		return;
	}
	ctx = sample_usbd_init_device(usb_msg_cb);
	if (ctx == NULL) {
		printk("USB CDC init failed\\n");
		return;
	}
	if (!usbd_can_detect_vbus(ctx)) {
		(void)usbd_enable(ctx);
	}
	printk("USB CDC ready\\n");
}
#endif

static const struct gpio_dt_spec heartbeat_led =
	GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

#if defined(CONFIG_HEIMDALL_BEACON)
_Static_assert(HEIMDALL_N_NODES >= 2 && HEIMDALL_N_NODES <= HEIMDALL_MAX_NODES,
	       "Heimdall node count is outside the protocol range");
_Static_assert(CONFIG_HEIMDALL_NODE_ID < HEIMDALL_N_NODES,
	       "Heimdall node ID is outside the configured roster range");
_Static_assert(HEIMDALL_CIR_TAPS >= 1 && HEIMDALL_CIR_TAPS <= HEIMDALL_MAX_CIR_TAPS,
	       "Heimdall CIR tap count is outside the protocol range");
_Static_assert(HEIMDALL_CIR_LEFT_TAPS < HEIMDALL_CIR_TAPS,
	       "Heimdall CIR window starts outside the tap count");
_Static_assert(HEIMDALL_MAX_FRAME_BYTES <= 1023,
	       "Heimdall maximum frame exceeds the DW3000 EXT PHR limit");
_Static_assert(HEIMDALL_FRAME_BYTES <= HEIMDALL_MAX_FRAME_BYTES,
	       "Heimdall frame exceeds the configured maximum");
_Static_assert(HEIMDALL_M * HEIMDALL_FRAME_PAYLOAD_BYTES >=
	       HEIMDALL_POOLED_REPORT_MAX_BYTES,
	       "Heimdall pooled report does not fit its frames");
_Static_assert(HEIMDALL_SLOT_DURATION_US % 100U == 0U,
	       "Heimdall slot duration is not quantised to 100 us");
_Static_assert(HEIMDALL_SLOT_DURATION_US >= HEIMDALL_SLOT_FLOOR_US,
	       "Heimdall slot duration is below the calculated floor");
#endif

static dwt_config_t radio_config = {
#if defined(CONFIG_HEIMDALL_BEACON)
	.chan = HEIMDALL_PHY_CHANNEL,
	.txPreambLength = HEIMDALL_PHY_PREAMBLE_LENGTH == 128 ? DWT_PLEN_128 : DWT_PLEN_64,
	.rxPAC = HEIMDALL_PHY_PAC == 8 ? DWT_PAC8 : DWT_PAC4,
	.txCode = HEIMDALL_PHY_TX_PREAMBLE_CODE,
	.rxCode = HEIMDALL_PHY_RX_PREAMBLE_CODE,
	.sfdType = HEIMDALL_PHY_SFD_TYPE,
	.dataRate = DWT_BR_6M8,
	.phrMode = HEIMDALL_PHY_PHR_MODE_EXT ? DWT_PHRMODE_EXT : DWT_PHRMODE_STD,
	.phrRate = HEIMDALL_PHY_PHR_RATE_DTA ? DWT_PHRRATE_DTA : DWT_PHRRATE_STD,
	.sfdTO = HEIMDALL_PHY_SFD_TIMEOUT,
	.stsMode = DWT_STS_MODE_OFF,
	.stsLength = DWT_STS_LEN_64,
	.pdoaMode = DWT_PDOA_M0,
#else
	.chan = 9,
	.txPreambLength = DWT_PLEN_128,
	.rxPAC = DWT_PAC8,
	.txCode = 9,
	.rxCode = 9,
	.sfdType = 1,
	.dataRate = DWT_BR_6M8,
	.phrMode = CONFIG_PHASE1_EXT_PHR ? DWT_PHRMODE_EXT : DWT_PHRMODE_STD,
	.phrRate = DWT_PHRRATE_STD,
	.sfdTO = 129,
	.stsMode = DWT_STS_MODE_OFF,
	.stsLength = DWT_STS_LEN_64,
	.pdoaMode = DWT_PDOA_M0,
#endif
};

static const dwt_txconfig_t tx_rf_config = {
#if defined(CONFIG_HEIMDALL_BEACON)
	.PGdly = HEIMDALL_PHY_TX_PG_DELAY,
	.power = HEIMDALL_PHY_TX_POWER,
#else
	.PGdly = 0x34,
	.power = 0xfefefefe,
#endif
	.PGcount = 0,
};

#if defined(CONFIG_PHASE1_ROLE_TX)
K_SEM_DEFINE(tx_done, 0, 1);
static volatile uint32_t tx_irq_count;

static void tx_confirm_cb(const dwt_cb_data_t *cb_data)
{
	ARG_UNUSED(cb_data);
	tx_irq_count++;
	k_sem_give(&tx_done);
}

static dwt_callbacks_s tx_callbacks = {
	.cbTxDone = tx_confirm_cb,
};
#elif defined(CONFIG_PHASE1_ROLE_RX)
static uint8_t rx_buffer[RX_BUFFER_LEN];
static volatile uint32_t rx_irq_count;
static volatile uint32_t rx_error_irq_count;
static volatile uint32_t rx_expected;
static volatile uint32_t rx_duplicates;
static volatile uint32_t rx_last_uptime_ms;
static volatile int16_t rx_last_rssi_q8_8;
static volatile int16_t rx_last_fp_power_q8_8;
static volatile bool rx_have_sequence;
static uint16_t rx_last_sequence;

static void rx_ok_cb(const dwt_cb_data_t *cb_data)
{
	dwt_cirdiags_t cir_diag;
	uint16_t sequence;
	uint16_t length = cb_data->datalength;

	if (length > sizeof(rx_buffer)) {
		length = sizeof(rx_buffer);
	}

	dwt_readrxdata(rx_buffer, length, 0);
	if ((length >= 6U) && (rx_buffer[0] == 0xC5) &&
	    (rx_buffer[4] == 'P') && (rx_buffer[5] == '1')) {
		sequence = (uint16_t)rx_buffer[2] |
			   ((uint16_t)rx_buffer[3] << 8);

		if ((sequence == 0U) && rx_have_sequence) {
			rx_irq_count = 0;
			rx_error_irq_count = 0;
			rx_expected = 0;
			rx_duplicates = 0;
			rx_have_sequence = false;
		}

		if (!rx_have_sequence) {
			rx_expected = (uint32_t)sequence + 1U;
			rx_have_sequence = true;
		} else if (sequence == rx_last_sequence) {
			rx_duplicates++;
		} else if (sequence > rx_last_sequence) {
			rx_expected += (uint32_t)(sequence - rx_last_sequence);
		}

		rx_last_sequence = sequence;
		rx_irq_count++;
		rx_last_uptime_ms = k_uptime_get_32();

		if (dwt_readdiagnostics_acc(&cir_diag, DWT_ACC_IDX_IP_M) ==
		    DWT_SUCCESS) {
			(void)dwt_calculate_rssi(&cir_diag, DWT_ACC_IDX_IP_M,
					 (int16_t *)&rx_last_rssi_q8_8);
			(void)dwt_calculate_first_path_power(
				&cir_diag, DWT_ACC_IDX_IP_M,
				(int16_t *)&rx_last_fp_power_q8_8);
		}
	}

	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
}

static void rx_error_cb(const dwt_cb_data_t *cb_data)
{
	ARG_UNUSED(cb_data);
	rx_error_irq_count++;
	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);
}

static dwt_callbacks_s rx_callbacks = {
	.cbRxOk = rx_ok_cb,
	.cbRxTo = rx_error_cb,
	.cbRxErr = rx_error_cb,
};
#endif

static int radio_probe_and_configure(bool configure_phy)
{
	int ret;
	int32_t probe_ret;
	uint32_t dev_id;
	uint32_t idle_wait;

	ret = dw3000_hw_init();
	printk("phase1: dw3000_hw_init=%d\n", ret);
	if (ret != 0) {
		return ret;
	}

	dw3000_hw_reset();
	k_msleep(10);

	probe_ret = dwt_probe((struct dwt_probe_s *)&dw3000_probe_interf);
	printk("phase1: dwt_probe=%d\n", probe_ret);
	if (probe_ret != DWT_SUCCESS) {
		printk("phase1: DEV_ID probe failed\n");
		return -EIO;
	}

	dev_id = dwt_readdevid();
	printk("phase1: DEV_ID=0x%08X\n", dev_id);

	if (!configure_phy) {
		return 0;
	}

	for (idle_wait = 0; idle_wait < 1000U; idle_wait++) {
		if (dwt_checkidlerc()) {
			break;
		}
		k_msleep(1);
	}
	if (idle_wait == 1000U) {
		printk("phase1: IDLE_RC timeout\n");
		return -ETIMEDOUT;
	}

	if (dwt_initialise(DWT_DW_INIT) == DWT_ERROR) {
		printk("phase1: dwt_initialise failed\n");
		return -EIO;
	}
	dw3000_spi_speed_fast();

	if (dwt_configure(&radio_config) == DWT_ERROR) {
		printk("phase1: dwt_configure failed\n");
		return -EIO;
	}

	dwt_configuretxrf((dwt_txconfig_t *)&tx_rf_config);
#if defined(CONFIG_PHASE1_ENABLE_FRAME_FILTER)
	dwt_setpanid(CONFIG_PHASE1_NETWORK_ID);
	dwt_setaddress16(CONFIG_PHASE1_NODE_ID);
	if (CONFIG_PHASE1_ENABLE_FRAME_FILTER) {
		uint16_t filter_flags = DWT_FF_DATA_EN;
#if CONFIG_PHASE1_FRAME_FILTER_EXTENDED
		filter_flags |= DWT_FF_EXTEND_EN;
#endif
		dwt_configureframefilter(DWT_FF_ENABLE_802_15_4, filter_flags);
	}
#endif
#if defined(CONFIG_HEIMDALL_BEACON)
	dwt_setpanid(HEIMDALL_NETWORK_ID);
	dwt_setaddress16(CONFIG_HEIMDALL_NODE_ID);
	if (HEIMDALL_ENABLE_FRAME_FILTER != 0U) {
		dwt_configureframefilter(DWT_FF_ENABLE_802_15_4, DWT_FF_DATA_EN);
	}
#endif
	/* Keep the DW3110 in IDLE_PLL between packets; do not enter IDLE_RC or
	 * sleep, so the RF synthesizer remains locked across the schedule. */
	if (dwt_setdwstate(DWT_DW_IDLE) == DWT_ERROR) {
		printk("phase1: IDLE_PLL request failed\n");
		return -EIO;
	}
	printk("phase1: PHY ch=9 prf=64 plen=128 rate=6M8 spi=%uMHz\n",
	       (uint32_t)(DT_PROP(DW3000_NODE, spi_max_frequency) / 1000000U));
	return 0;
}

static int run_tx(void)
{
#if defined(CONFIG_PHASE1_ROLE_TX)
	uint8_t tx_frame[FRAME_DATA_LEN] = {
		0xC5, 0, 0, 0, 'P', '1', 'U', 'W', 'B', '!'
	};
	uint32_t sequence;
	uint32_t start_failures = 0;
	uint32_t irq_timeouts = 0;

	dwt_setcallbacks(&tx_callbacks);
	dwt_setinterrupt(DWT_INT_TXFRS_BIT_MASK, 0, DWT_ENABLE_INT);
	dwt_writesysstatuslo(DWT_INT_RCINIT_BIT_MASK | DWT_INT_SPIRDY_BIT_MASK);
	if (dw3000_hw_init_interrupt() != 0) {
		printk("phase1: IRQ init failed\n");
		return -EIO;
	}

	printk("phase1: TX_START frames=%u period_ms=%u mode=IRQ\n",
	       FRAME_COUNT, TX_PERIOD_MS);
	for (sequence = 0; sequence < FRAME_COUNT; sequence++) {
		tx_frame[1] = (uint8_t)sequence;
		tx_frame[2] = (uint8_t)sequence;
		tx_frame[3] = (uint8_t)(sequence >> 8);
		dwt_writetxdata(FRAME_DATA_LEN, tx_frame, 0);
		dwt_writetxfctrl(FRAME_WIRE_LEN, 0, 0);

		if (dwt_starttx(DWT_START_TX_IMMEDIATE) == DWT_ERROR) {
			start_failures++;
		} else if (k_sem_take(&tx_done, K_MSEC(100)) != 0) {
			irq_timeouts++;
		}

		if (((sequence + 1U) % 100U) == 0U) {
			printk("phase1: TX_PROGRESS sent=%u irq=%u start_fail=%u irq_to=%u\n",
			       sequence + 1U, tx_irq_count, start_failures,
			       irq_timeouts);
		}
		k_msleep(TX_PERIOD_MS);
	}

	printk("phase1: TX_FINAL sent=%u irq=%u start_fail=%u irq_to=%u\n",
	       FRAME_COUNT, tx_irq_count, start_failures, irq_timeouts);
	return 0;
#else
	return -ENOTSUP;
#endif
}

static int run_rx(void)
{
#if defined(CONFIG_PHASE1_ROLE_RX)
	uint32_t last_printed = 0;
	bool final_printed = false;

	dwt_setcallbacks(&rx_callbacks);
	dwt_setinterrupt(DWT_INT_RXFCG_BIT_MASK | SYS_STATUS_ALL_RX_ERR,
			 0, DWT_ENABLE_INT);
	dwt_writesysstatuslo(DWT_INT_RCINIT_BIT_MASK | DWT_INT_SPIRDY_BIT_MASK);
	if (dw3000_hw_init_interrupt() != 0) {
		printk("phase1: IRQ init failed\n");
		return -EIO;
	}

	printk("phase1: RX_START mode=IRQ\n");
	(void)dwt_rxenable(DWT_START_RX_IMMEDIATE);

	while (true) {
		uint32_t received = rx_irq_count;
		uint32_t expected = rx_expected;

		if ((received != last_printed) &&
		    ((received % 100U) == 0U)) {
			printk("phase1: RX_PROGRESS received=%u expected=%u errors=%u rssi_q8_8=%d fp_q8_8=%d\n",
			       received, expected, rx_error_irq_count,
			       rx_last_rssi_q8_8, rx_last_fp_power_q8_8);
			last_printed = received;
		}

		if (rx_have_sequence && !final_printed &&
		    ((uint32_t)(k_uptime_get_32() - rx_last_uptime_ms) > 1000U)) {
			uint32_t unique = received - rx_duplicates;
			uint32_t lost = FRAME_COUNT > unique ?
				FRAME_COUNT - unique : 0U;
			uint32_t rate_x100 = (unique * 10000U) / FRAME_COUNT;
			printk("phase1: RX_FINAL received=%u expected=%u lost=%u duplicates=%u errors=%u rate=%u.%02u%% irq=%u rssi_q8_8=%d fp_q8_8=%d\n",
			       unique, FRAME_COUNT, lost, rx_duplicates,
			       rx_error_irq_count, rate_x100 / 100U,
			       rate_x100 % 100U, received,
			       rx_last_rssi_q8_8, rx_last_fp_power_q8_8);
			final_printed = true;
		}

		if (rx_have_sequence &&
		    ((uint32_t)(k_uptime_get_32() - rx_last_uptime_ms) < 100U)) {
			final_printed = false;
		}
		k_msleep(20);
	}
#else
	return -ENOTSUP;
#endif
}

int main(void)
{
	#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
	usb_init();
	#endif
	int ret;
	uint32_t heartbeat = 0;

	if (!gpio_is_ready_dt(&heartbeat_led)) {
		printk("phase1: D9 GPIO is not ready\n");
		return 0;
	}

	ret = gpio_pin_configure_dt(&heartbeat_led, GPIO_OUTPUT_INACTIVE);
	if (ret != 0) {
		printk("phase1: D9 configure failed: %d\n", ret);
		return 0;
	}

	printk("phase1: hello from DWM3001CDK Zephyr bring-up\n");
	ret = radio_probe_and_configure(
#if defined(CONFIG_PHASE1_ROLE_DEV_ID)
		false
#else
		true
#endif
	);
	if (ret != 0) {
		return 0;
	}

#if defined(CONFIG_PHASE1_ROLE_TX)
	(void)run_tx();
#elif defined(CONFIG_PHASE1_ROLE_RX)
	(void)run_rx();
#elif defined(CONFIG_PHASE1_ROLE_SCHEDULED_TX)
	(void)phase1_run_scheduled_tx();
#elif defined(CONFIG_PHASE1_ROLE_SENSING_RX)
	(void)phase1_run_sensing_rx();
#elif defined(CONFIG_PHASE1_ROLE_TWR_INITIATOR)
	(void)phase1_run_twr_initiator();
#elif defined(CONFIG_PHASE1_ROLE_TWR_RESPONDER)
	(void)phase1_run_twr_responder();
#endif

	while (true) {
		(void)gpio_pin_toggle_dt(&heartbeat_led);
		printk("phase1: heartbeat %u\n", heartbeat++);
		k_msleep(HEARTBEAT_PERIOD_MS);
	}

	return 0;
}
