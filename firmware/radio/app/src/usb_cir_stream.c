#include "usb_cir_stream.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>

#define CIR_LINE_MAX 300
#define CIR_QUEUE_LEN 32

struct cir_message {
	uint16_t length;
	uint8_t data[CIR_LINE_MAX];
};

K_MSGQ_DEFINE(cir_queue, sizeof(struct cir_message), CIR_QUEUE_LEN, 4);

static const struct device *const cdc_uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
#if defined(CONFIG_PHASE1_ROLE_USB_THROUGHPUT)
K_SEM_DEFINE(cdc_ready, 0, 1);
#endif
#if defined(CONFIG_PHASE1_USB_BULK_TX)
static atomic_t cdc_irq_ready;
static struct cir_message tx_message;
static uint16_t tx_offset;
static bool tx_message_active;
#endif

void live_cir_stream_enqueue(const char *line, size_t length)
{
	struct cir_message message;

	if (length >= CIR_LINE_MAX - 2U) {
		return;
	}
	message.length = (uint16_t)length;
	memcpy(message.data, line, length);
	message.data[length++] = '\r';
	message.data[length++] = '\n';
	message.length = (uint16_t)length;
	if (k_msgq_put(&cir_queue, &message, K_NO_WAIT) == 0
#if defined(CONFIG_PHASE1_USB_BULK_TX)
	    && atomic_get(&cdc_irq_ready) != 0
#endif
	) {
#if defined(CONFIG_PHASE1_USB_BULK_TX)
		uart_irq_tx_enable(cdc_uart);
#endif
	}
}

static int32_t sign_extend_18(const uint8_t *p)
{
	uint32_t v = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
			((uint32_t)p[2] << 16);
	v &= 0x3ffffU;
	return (v & 0x20000U) ? (int32_t)(v | 0xfffc0000U) : (int32_t)v;
}

bool live_cir_stream_enqueue_binary(uint32_t seq, uint64_t rx_ts, int32_t cfo,
                                    uint32_t fp, uint8_t agc_state,
                                    int16_t rssi_q8_8, int16_t fp_power_q8_8,
                                    const uint8_t *cir_raw)
{
	struct cir_message message;
	uint8_t *p = message.data;

	/* CIR2 adds DGC, RSSI, and first-path power after the PHY metadata. */
	memcpy(p, "CIR2", 4); p += 4;
	sys_put_le32(seq, p); p += 4;
	sys_put_le64(rx_ts, p); p += 8;
	sys_put_le32((uint32_t)cfo, p); p += 4;
	sys_put_le32(fp, p); p += 4;
	*p++ = agc_state;
	sys_put_le16((uint16_t)rssi_q8_8, p); p += 2;
	sys_put_le16((uint16_t)fp_power_q8_8, p); p += 2;
	for (int i = 0; i < 64; ++i) {
		sys_put_le16((uint16_t)(sign_extend_18(&cir_raw[i * 6]) >> 2), p); p += 2;
		sys_put_le16((uint16_t)(sign_extend_18(&cir_raw[i * 6 + 3]) >> 2), p); p += 2;
	}
	message.length = (uint16_t)(p - message.data);
	bool queued = k_msgq_put(&cir_queue, &message, K_NO_WAIT) == 0;

	if (queued
#if defined(CONFIG_PHASE1_USB_BULK_TX)
	    && atomic_get(&cdc_irq_ready) != 0
#endif
	) {
#if defined(CONFIG_PHASE1_USB_BULK_TX)
		uart_irq_tx_enable(cdc_uart);
#endif
	}
	return queued;
}

#if defined(CONFIG_PHASE1_USB_BULK_TX)
static void cir_stream_uart_irq(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	if (!uart_irq_update(dev) || !uart_irq_tx_ready(dev)) {
		return;
	}

	while (true) {
		if (!tx_message_active) {
			if (k_msgq_get(&cir_queue, &tx_message, K_NO_WAIT) != 0) {
				uart_irq_tx_disable(dev);
				return;
			}
			tx_offset = 0U;
			tx_message_active = true;
		}

		int sent = uart_fifo_fill(dev, &tx_message.data[tx_offset],
					  tx_message.length - tx_offset);
		if (sent <= 0) {
			return;
		}
		tx_offset += (uint16_t)sent;
		if (tx_offset == tx_message.length) {
			tx_message_active = false;
		}
	}
}
#endif

#if defined(CONFIG_PHASE1_ROLE_USB_THROUGHPUT)
static int live_cir_stream_wait_ready(k_timeout_t timeout)
{
	return k_sem_take(&cdc_ready, timeout);
}

int phase1_run_usb_throughput(void)
{
	static uint8_t cir_raw[64U * 6U];
	uint32_t enqueued = 0U;
	uint32_t dropped = 0U;

	printk("phase1: USB_START records=%u period_us=%u record_bytes=285\n",
	       CONFIG_PHASE1_USB_TEST_RECORDS, CONFIG_PHASE1_USB_TEST_PERIOD_US);
	if (live_cir_stream_wait_ready(K_SECONDS(5)) != 0) {
		printk("phase1: USB_READY timeout\n");
		return -ETIMEDOUT;
	}
	k_msleep(CONFIG_PHASE1_USB_TEST_START_DELAY_MS);
	for (uint32_t sequence = 0U;
	     sequence < CONFIG_PHASE1_USB_TEST_RECORDS; sequence++) {
		if (live_cir_stream_enqueue_binary(sequence, 0U, 0, 0U, 0U, 0, 0,
						   cir_raw)) {
			enqueued++;
		} else {
			dropped++;
		}
		k_usleep(CONFIG_PHASE1_USB_TEST_PERIOD_US);
	}

	printk("phase1: USB_FINAL attempted=%u enqueued=%u dropped=%u\n",
	       CONFIG_PHASE1_USB_TEST_RECORDS, enqueued, dropped);
	return 0;
}
#endif

static void cir_stream_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (!device_is_ready(cdc_uart)) {
		k_sleep(K_MSEC(10));
	}
	#if defined(CONFIG_PHASE1_USB_BULK_TX)
	if (uart_irq_callback_user_data_set(cdc_uart,
						    cir_stream_uart_irq,
						    NULL) != 0) {
		printk("phase1: USB IRQ callback setup failed\n");
		return;
	}
	atomic_set(&cdc_irq_ready, 1);
	#endif
#if defined(CONFIG_PHASE1_ROLE_USB_THROUGHPUT)
	k_sem_give(&cdc_ready);
#endif
#if defined(CONFIG_PHASE1_USB_BULK_TX)
	if (k_msgq_num_used_get(&cir_queue) != 0U) {
		uart_irq_tx_enable(cdc_uart);
	}
#endif

#if defined(CONFIG_PHASE1_USB_BULK_TX)
	while (true) {
		k_sleep(K_FOREVER);
	}
#else
	while (true) {
		struct cir_message message;
		k_msgq_get(&cir_queue, &message, K_FOREVER);
		if (!device_is_ready(cdc_uart)) {
			continue;
		}
		for (uint16_t i = 0; i < message.length; ++i) {
			uart_poll_out(cdc_uart, message.data[i]);
		}
	}
#endif
}

K_THREAD_DEFINE(cir_stream_tid, 2048, cir_stream_thread, NULL, NULL, NULL,
			5, 0, 0);
