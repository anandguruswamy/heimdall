#include "usb_cir_stream.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
#include "heimdall_beacon_config.h"
#include "beacon_wire.h"
#endif

#define STREAM_QUEUE_LEN 32
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
#define STREAM_DATA_MAX (HEIMDALL_FRAME_BYTES + 22U)
#else
#define STREAM_DATA_MAX 300U
#endif

struct stream_message {
	void *fifo_reserved;
	uint16_t length;
	uint8_t data[STREAM_DATA_MAX];
};

K_MEM_SLAB_DEFINE(stream_slab, sizeof(struct stream_message), STREAM_QUEUE_LEN, 4);
K_FIFO_DEFINE(stream_fifo);

static const struct device *const cdc_uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
#if defined(CONFIG_PHASE1_ROLE_USB_THROUGHPUT)
K_SEM_DEFINE(cdc_ready, 0, 1);
#endif
#if defined(CONFIG_PHASE1_USB_BULK_TX)
static atomic_t cdc_irq_ready;
static struct stream_message *tx_message;
static uint16_t tx_offset;
#endif
static atomic_t stream_drop_count;
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
static atomic_t heimdall_record_sequence;
static atomic_t heimdall_drop_pending;
#endif

static struct stream_message *stream_message_alloc(void)
{
	struct stream_message *message;

	if (k_mem_slab_alloc(&stream_slab, (void **)&message, K_NO_WAIT) != 0) {
		atomic_inc(&stream_drop_count);
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
		atomic_set(&heimdall_drop_pending, 1);
#endif
		return NULL;
	}
	return message;
}

static void stream_message_submit(struct stream_message *message)
{
	k_fifo_put(&stream_fifo, message);
#if defined(CONFIG_PHASE1_USB_BULK_TX)
	if (atomic_get(&cdc_irq_ready) != 0) {
		uart_irq_tx_enable(cdc_uart);
	}
#endif
}

void live_cir_stream_enqueue(const char *line, size_t length)
{
	struct stream_message *message;

	if (length >= STREAM_DATA_MAX - 2U) {
		atomic_inc(&stream_drop_count);
		return;
	}
	message = stream_message_alloc();
	if (message == NULL) {
		return;
	}
	memcpy(message->data, line, length);
	message->data[length++] = '\r';
	message->data[length++] = '\n';
	message->length = (uint16_t)length;
	stream_message_submit(message);
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
	struct stream_message *message = stream_message_alloc();
	uint8_t *p;

	if (message == NULL) {
		return false;
	}
	p = message->data;

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
	message->length = (uint16_t)(p - message->data);
	stream_message_submit(message);
	return true;
}

bool heimdall_usb_enqueue_record(uint8_t type, uint8_t flags,
				 const void *prefix, uint16_t prefix_length,
				 const void *body, uint16_t body_length)
{
#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
	struct stream_message *message;
	uint16_t payload_length = prefix_length + body_length;
	uint16_t record_length = 16U + payload_length;
	uint32_t sequence = (uint32_t)atomic_inc(&heimdall_record_sequence);
	uint8_t *payload;

	if (record_length > STREAM_DATA_MAX ||
	    (prefix_length != 0U && prefix == NULL) ||
	    (body_length != 0U && body == NULL)) {
		atomic_inc(&stream_drop_count);
		atomic_set(&heimdall_drop_pending, 1);
		return false;
	}
	message = stream_message_alloc();
	if (message == NULL) {
		return false;
	}
	if (atomic_cas(&heimdall_drop_pending, 1, 0)) {
		flags |= BIT(0);
	}

	sys_put_le16(0xA5C3U, &message->data[0]);
	message->data[2] = 1U;
	message->data[3] = type;
	message->data[4] = flags;
	message->data[5] = 0U;
	sys_put_le16(payload_length, &message->data[6]);
	sys_put_le32(sequence, &message->data[8]);
	payload = &message->data[12];
	if (prefix_length != 0U) {
		memcpy(payload, prefix, prefix_length);
	}
	if (body_length != 0U) {
		memcpy(payload + prefix_length, body, body_length);
	}
	sys_put_le32(heimdall_crc32(&message->data[2], 10U + payload_length),
		     &message->data[12U + payload_length]);
	message->length = record_length;
	stream_message_submit(message);
	return true;
#else
	ARG_UNUSED(type);
	ARG_UNUSED(flags);
	ARG_UNUSED(prefix);
	ARG_UNUSED(prefix_length);
	ARG_UNUSED(body);
	ARG_UNUSED(body_length);
	return false;
#endif
}

uint32_t heimdall_usb_drop_count_get(void)
{
	return (uint32_t)atomic_get(&stream_drop_count);
}

void heimdall_usb_drop_count_ack(uint32_t count)
{
	atomic_sub(&stream_drop_count, (atomic_val_t)count);
}

#if defined(CONFIG_PHASE1_USB_BULK_TX)
static void cir_stream_uart_irq(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	if (!uart_irq_update(dev) || !uart_irq_tx_ready(dev)) {
		return;
	}

	while (true) {
		if (tx_message == NULL) {
			tx_message = k_fifo_get(&stream_fifo, K_NO_WAIT);
			if (tx_message == NULL) {
				uart_irq_tx_disable(dev);
				if (!k_fifo_is_empty(&stream_fifo)) {
					uart_irq_tx_enable(dev);
				}
				return;
			}
			tx_offset = 0U;
		}

		int sent = uart_fifo_fill(dev, &tx_message->data[tx_offset],
					  tx_message->length - tx_offset);
		if (sent <= 0) {
			return;
		}
		tx_offset += (uint16_t)sent;
		if (tx_offset == tx_message->length) {
			k_mem_slab_free(&stream_slab, tx_message);
			tx_message = NULL;
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
	if (!k_fifo_is_empty(&stream_fifo)) {
		uart_irq_tx_enable(cdc_uart);
	}
#endif

#if defined(CONFIG_PHASE1_USB_BULK_TX)
	while (true) {
		k_sleep(K_FOREVER);
	}
#else
	while (true) {
		struct stream_message *message = k_fifo_get(&stream_fifo, K_FOREVER);
		if (!device_is_ready(cdc_uart)) {
			k_mem_slab_free(&stream_slab, message);
			continue;
		}
		for (uint16_t i = 0; i < message->length; ++i) {
			uart_poll_out(cdc_uart, message->data[i]);
		}
		k_mem_slab_free(&stream_slab, message);
	}
#endif
}

K_THREAD_DEFINE(cir_stream_tid, 2048, cir_stream_thread, NULL, NULL, NULL,
			5, 0, 0);
