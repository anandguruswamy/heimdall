#pragma once

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#include <zephyr/sys/atomic.h>

struct heimdall_usb_queue_diagnostics {
	atomic_t allocation_failures;
	atomic_t depth;
	atomic_t depth_high_water;
	atomic_t callback_count;
	atomic_t fill_calls;
	atomic_t fill_zero_returns;
	atomic_t bytes_accepted;
};

extern struct heimdall_usb_queue_diagnostics heimdall_usb_queue_diagnostics;

void live_cir_stream_enqueue(const char *line, size_t length);
bool live_cir_stream_enqueue_binary(uint32_t seq, uint64_t rx_ts, int32_t cfo,
                                    uint32_t fp, uint8_t agc_state,
                                    int16_t rssi_q8_8, int16_t fp_power_q8_8,
                                    const uint8_t *cir_raw);

int phase1_run_usb_throughput(void);

bool heimdall_usb_enqueue_record(uint8_t type, uint8_t flags,
				 const void *prefix, uint16_t prefix_length,
				 const void *body, uint16_t body_length);
uint32_t heimdall_usb_drop_count_get(void);
uint32_t heimdall_usb_drop_count_take(void);
void heimdall_usb_drop_count_restore(uint32_t count);
void heimdall_usb_note_drop(void);
