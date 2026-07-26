#include "beacon_report.h"

#include <errno.h>
#include <string.h>

#include "beacon_schedule.h"

int heimdall_report_pack(struct heimdall_report *report, uint32_t k,
				 uint8_t reporting_node, uint8_t n_nodes,
				 const uint8_t *subreports[HEIMDALL_REPORT_NODE_SLOTS],
				 const uint16_t subreport_lengths[HEIMDALL_REPORT_NODE_SLOTS])
{
	uint16_t offset = 0U;

	if (report == NULL || subreports == NULL || subreport_lengths == NULL ||
	    n_nodes < 2U || n_nodes > 8U || reporting_node >= n_nodes) {
		return -EINVAL;
	}
	memset(report->start_offsets, 0, sizeof(report->start_offsets));
	memset(report->ordered_nodes, 0, sizeof(report->ordered_nodes));
	report->subreport_count = 0U;
	report->total_bytes = 0U;
	report->peer_observed_bitmap = 0U;

	for (uint8_t ordinal = 0U; ordinal < n_nodes - 1U; ++ordinal) {
		uint8_t observed = heimdall_schedule_order(k, reporting_node, n_nodes, ordinal);
		uint16_t length = subreport_lengths[observed];

		if (subreports[observed] == NULL || length == 0U) {
			continue;
		}
		if ((size_t)offset + length > sizeof(report->bytes) ||
		    report->subreport_count >= HEIMDALL_REPORT_MAX_SUBREPORTS) {
			return -E2BIG;
		}
		report->start_offsets[report->subreport_count] = offset;
		report->ordered_nodes[report->subreport_count] = observed;
		memcpy(&report->bytes[offset], subreports[observed], length);
		offset += length;
		report->peer_observed_bitmap |= (uint8_t)(1U << observed);
		report->subreport_count++;
	}

	report->total_bytes = offset;
	return 0;
}

uint8_t heimdall_report_frame_subreport_count(const struct heimdall_report *report,
						       uint8_t frame_index,
						       uint16_t frame_payload_bytes)
{
	uint32_t start;
	uint32_t end;
	uint8_t count = 0U;

	if (report == NULL || frame_payload_bytes == 0U) {
		return 0U;
	}
	start = (uint32_t)frame_index * frame_payload_bytes;
	end = start + frame_payload_bytes;
	for (uint8_t i = 0U; i < report->subreport_count; ++i) {
		if (report->start_offsets[i] >= start && report->start_offsets[i] < end) {
			count++;
		}
	}
	return count;
}

int heimdall_report_copy_frame(const struct heimdall_report *report,
				       uint8_t frame_index, uint16_t frame_payload_bytes,
				       uint8_t *out, size_t out_length)
{
	uint32_t offset;
	size_t available;

	if (report == NULL || out == NULL || frame_payload_bytes == 0U ||
	    out_length < frame_payload_bytes) {
		return -EINVAL;
	}
	offset = (uint32_t)frame_index * frame_payload_bytes;
	memset(out, 0, frame_payload_bytes);
	if (offset >= report->total_bytes) {
		return 0;
	}
	available = report->total_bytes - offset;
	if (available > frame_payload_bytes) {
		available = frame_payload_bytes;
	}
	memcpy(out, &report->bytes[offset], available);
	return 0;
}
