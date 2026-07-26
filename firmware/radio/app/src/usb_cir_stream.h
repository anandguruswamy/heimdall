#pragma once

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

void live_cir_stream_enqueue(const char *line, size_t length);
bool live_cir_stream_enqueue_binary(uint32_t seq, uint64_t rx_ts, int32_t cfo,
                                    uint32_t fp, uint8_t agc_state,
                                    int16_t rssi_q8_8, int16_t fp_power_q8_8,
                                    const uint8_t *cir_raw);

int phase1_run_usb_throughput(void);
