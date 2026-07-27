#include "beacon_wire.h"

#include <errno.h>
#include <string.h>

#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>

static uint32_t crc32_table[256];
static bool crc32_table_ready;

static void crc32_table_init(void)
{
	if (crc32_table_ready) {
		return;
	}
	for (uint32_t i = 0U; i < 256U; ++i) {
		uint32_t value = i;

		for (uint8_t bit = 0U; bit < 8U; ++bit) {
			value = (value >> 1) ^
				((value & 1U) != 0U ? 0xedb88320U : 0U);
		}
		crc32_table[i] = value;
	}
	crc32_table_ready = true;
}

#if defined(CONFIG_HEIMDALL_RUNTIME_GATEWAY)
void heimdall_crc32_init(void)
{
	crc32_table_init();
}
#endif

static uint64_t get_le40(const uint8_t *data)
{
	uint64_t value = 0U;

	for (size_t i = 0U; i < 5U; ++i) {
		value |= (uint64_t)data[i] << (i * 8U);
	}
	return value;
}

static void put_le40(uint8_t *data, uint64_t value)
{
	value &= ((1ULL << 40U) - 1U);
	for (size_t i = 0U; i < 5U; ++i) {
		data[i] = (uint8_t)(value >> (i * 8U));
	}
}

uint32_t heimdall_crc32(const uint8_t *data, size_t length)
{
	uint32_t crc = UINT32_MAX;

	crc32_table_init();
	for (size_t i = 0U; i < length; ++i) {
		crc = (crc >> 8) ^ crc32_table[(crc ^ data[i]) & 0xffU];
	}
	return ~crc;
}

int heimdall_frame_header_encode(const struct heimdall_frame_header *header,
					 uint8_t *out, size_t out_length)
{
	if (header == NULL || out == NULL || out_length < HEIMDALL_FRAME_HEADER_BYTES) {
		return -EINVAL;
	}

	sys_put_le16(0x8841U, &out[0]);
	out[2] = header->mac_seq;
	sys_put_le16(header->network_id, &out[3]);
	sys_put_le16(0xFFFFU, &out[5]);
	sys_put_le16(header->src_addr, &out[7]);
	out[9] = header->protocol_version;
	out[10] = header->frame_type;
	out[11] = header->m;
	sys_put_le32(header->k, &out[12]);
	out[16] = header->n_nodes;
	out[17] = header->slots_per_superslot;
	sys_put_le16(header->config_hash, &out[18]);
	put_le40(&out[20], header->tx_timestamp);
	out[25] = header->subreport_count;
	sys_put_le16(header->pooled_total_bytes, &out[26]);
	out[28] = header->peer_observed_bitmap;
	out[29] = header->evidence_age;
	out[30] = header->flags;
	return 0;
}

int heimdall_frame_header_decode(struct heimdall_frame_header *header,
					 const uint8_t *data, size_t data_length)
{
	if (header == NULL || data == NULL || data_length < HEIMDALL_FRAME_HEADER_BYTES) {
		return -EINVAL;
	}
	if (sys_get_le16(&data[0]) != 0x8841U || sys_get_le16(&data[5]) != 0xFFFFU) {
		return -EBADMSG;
	}

	header->mac_seq = data[2];
	header->network_id = sys_get_le16(&data[3]);
	header->src_addr = (uint8_t)sys_get_le16(&data[7]);
	header->protocol_version = data[9];
	header->frame_type = data[10];
	header->m = data[11];
	header->k = sys_get_le32(&data[12]);
	header->n_nodes = data[16];
	header->slots_per_superslot = data[17];
	header->config_hash = sys_get_le16(&data[18]);
	header->tx_timestamp = get_le40(&data[20]);
	header->subreport_count = data[25];
	header->pooled_total_bytes = sys_get_le16(&data[26]);
	header->peer_observed_bitmap = data[28];
	header->evidence_age = data[29];
	header->flags = data[30];
	return 0;
}

int heimdall_subreport_encode(const struct heimdall_subreport *subreport,
				      uint8_t *out, size_t out_length)
{
	size_t cir_bytes;
	size_t crc_offset;

	if (subreport == NULL || out == NULL || subreport->cir_iq == NULL ||
	    subreport->cir_taps == 0U || subreport->cir_taps > HEIMDALL_MAX_CIR_TAPS) {
		return -EINVAL;
	}
	cir_bytes = (size_t)subreport->cir_taps * 4U;
	crc_offset = 36U + cir_bytes;
	if (out_length < crc_offset + 4U) {
		return -EINVAL;
	}

	out[0] = subreport->observed_node_id;
	out[1] = subreport->obs_flags;
	out[2] = subreport->observed_m;
	out[3] = subreport->round_delta;
	put_le40(&out[4], subreport->observed_tx_timestamp);
	put_le40(&out[9], subreport->rx_timestamp);
	sys_put_le16((uint16_t)subreport->cfo_raw, &out[14]);
	sys_put_le16(subreport->fp_index_q10_6, &out[16]);
	 sys_put_le24(subreport->f1, &out[18]);
	 sys_put_le24(subreport->f2, &out[21]);
	 sys_put_le24(subreport->f3, &out[24]);
	 sys_put_le24(subreport->ip_power, &out[27]);
	sys_put_le16(subreport->accum_count, &out[30]);
	out[32] = subreport->dgc_decision;
	sys_put_le16(subreport->cir_start_offset, &out[33]);
	out[35] = subreport->cir_taps;
	for (size_t i = 0U; i < (size_t)subreport->cir_taps * 2U; ++i) {
		sys_put_le16((uint16_t)subreport->cir_iq[i], &out[36U + i * 2U]);
	}
	sys_put_le32(heimdall_crc32(out, crc_offset), &out[crc_offset]);
	return 0;
}

int heimdall_subreport_decode(struct heimdall_subreport *subreport,
				      int16_t *cir_iq, size_t cir_iq_count,
				      const uint8_t *data, size_t data_length)
{
	size_t cir_bytes;
	size_t crc_offset;

	if (subreport == NULL || cir_iq == NULL || data == NULL || data_length < 40U) {
		return -EINVAL;
	}
	subreport->cir_taps = data[35];
	if (subreport->cir_taps == 0U || subreport->cir_taps > HEIMDALL_MAX_CIR_TAPS) {
		return -EBADMSG;
	}
	cir_bytes = (size_t)subreport->cir_taps * 4U;
	crc_offset = 36U + cir_bytes;
	if (cir_iq_count < (size_t)subreport->cir_taps * 2U || data_length < crc_offset + 4U ||
	    sys_get_le32(&data[crc_offset]) != heimdall_crc32(data, crc_offset)) {
		return -EBADMSG;
	}

	subreport->observed_node_id = data[0];
	subreport->obs_flags = data[1];
	subreport->observed_m = data[2];
	subreport->round_delta = data[3];
	subreport->observed_tx_timestamp = get_le40(&data[4]);
	subreport->rx_timestamp = get_le40(&data[9]);
	subreport->cfo_raw = (int16_t)sys_get_le16(&data[14]);
	subreport->fp_index_q10_6 = sys_get_le16(&data[16]);
	subreport->f1 = sys_get_le24(&data[18]);
	subreport->f2 = sys_get_le24(&data[21]);
	subreport->f3 = sys_get_le24(&data[24]);
	subreport->ip_power = sys_get_le24(&data[27]);
	subreport->accum_count = sys_get_le16(&data[30]);
	subreport->dgc_decision = data[32];
	subreport->cir_start_offset = sys_get_le16(&data[33]);
	subreport->cir_iq = cir_iq;
	for (size_t i = 0U; i < (size_t)subreport->cir_taps * 2U; ++i) {
		cir_iq[i] = (int16_t)sys_get_le16(&data[36U + i * 2U]);
	}
	return 0;
}
