#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifndef HEIMDALL_FRAME_HEADER_BYTES
#define HEIMDALL_FRAME_HEADER_BYTES 31U
#endif
#define HEIMDALL_SUBREPORT_FIXED_BYTES 40U
#define HEIMDALL_MAX_CIR_TAPS 128U

struct heimdall_frame_header {
	uint8_t mac_seq;
	uint16_t network_id;
	uint8_t src_addr;
	uint8_t protocol_version;
	uint8_t frame_type;
	uint8_t m;
	uint32_t k;
	uint8_t n_nodes;
	uint8_t slots_per_superslot;
	uint16_t config_hash;
	uint64_t tx_timestamp;
	uint8_t subreport_count;
	uint16_t pooled_total_bytes;
	uint8_t peer_observed_bitmap;
	uint8_t evidence_age;
	uint8_t flags;
};

struct heimdall_subreport {
	uint8_t observed_node_id;
	uint8_t obs_flags;
	uint8_t observed_m;
	uint8_t round_delta;
	uint64_t observed_tx_timestamp;
	uint64_t rx_timestamp;
	int16_t cfo_raw;
	uint16_t fp_index_q10_6;
	uint32_t f1;
	uint32_t f2;
	uint32_t f3;
	uint32_t ip_power;
	uint16_t accum_count;
	uint8_t dgc_decision;
	uint16_t cir_start_offset;
	uint8_t cir_taps;
	const int16_t *cir_iq;
};

uint32_t heimdall_crc32(const uint8_t *data, size_t length);
void heimdall_crc32_init(void);

int heimdall_frame_header_encode(const struct heimdall_frame_header *header,
					 uint8_t *out, size_t out_length);
int heimdall_frame_header_decode(struct heimdall_frame_header *header,
					 const uint8_t *data, size_t data_length);

int heimdall_subreport_encode(const struct heimdall_subreport *subreport,
				      uint8_t *out, size_t out_length);
int heimdall_subreport_decode(struct heimdall_subreport *subreport,
				      int16_t *cir_iq, size_t cir_iq_count,
				      const uint8_t *data, size_t data_length);
