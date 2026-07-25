#include "usb_cir_stream.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>

#define CIR_LINE_MAX 300
#define CIR_QUEUE_LEN 8

struct cir_message {
	uint16_t length;
	uint8_t data[CIR_LINE_MAX];
};

K_MSGQ_DEFINE(cir_queue, sizeof(struct cir_message), CIR_QUEUE_LEN, 4);

static const struct device *const cdc_uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);

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
	(void)k_msgq_put(&cir_queue, &message, K_NO_WAIT);
}

static int32_t sign_extend_18(const uint8_t *p)
{
	uint32_t v = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
			((uint32_t)p[2] << 16);
	v &= 0x3ffffU;
	return (v & 0x20000U) ? (int32_t)(v | 0xfffc0000U) : (int32_t)v;
}

void live_cir_stream_enqueue_binary(uint32_t seq, uint64_t rx_ts, int32_t cfo,
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
	(void)k_msgq_put(&cir_queue, &message, K_NO_WAIT);
}

static void cir_stream_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

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
}

K_THREAD_DEFINE(cir_stream_tid, 2048, cir_stream_thread, NULL, NULL, NULL,
			5, 0, 0);
