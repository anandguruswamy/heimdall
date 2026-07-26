#pragma once

#include <stddef.h>
#include <stdint.h>

#define HEIMDALL_REPORT_MAX_BYTES (7U * 552U)
#define HEIMDALL_REPORT_MAX_SUBREPORTS 7U
#define HEIMDALL_REPORT_NODE_SLOTS 8U

struct heimdall_report {
	uint8_t bytes[HEIMDALL_REPORT_MAX_BYTES];
	uint16_t start_offsets[HEIMDALL_REPORT_MAX_SUBREPORTS];
	uint8_t ordered_nodes[HEIMDALL_REPORT_MAX_SUBREPORTS];
	uint8_t subreport_count;
	uint16_t total_bytes;
	uint8_t peer_observed_bitmap;
};

int heimdall_report_pack(struct heimdall_report *report, uint32_t k,
				 uint8_t reporting_node, uint8_t n_nodes,
				 const uint8_t *subreports[HEIMDALL_REPORT_NODE_SLOTS],
				 const uint16_t subreport_lengths[HEIMDALL_REPORT_NODE_SLOTS]);

uint8_t heimdall_report_frame_subreport_count(const struct heimdall_report *report,
						       uint8_t frame_index,
						       uint16_t frame_payload_bytes);

int heimdall_report_copy_frame(const struct heimdall_report *report,
				       uint8_t frame_index, uint16_t frame_payload_bytes,
				       uint8_t *out, size_t out_length);
