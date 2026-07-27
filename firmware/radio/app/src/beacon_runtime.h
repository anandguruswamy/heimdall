#pragma once

#include <stdint.h>

#include <zephyr/sys/atomic.h>

struct heimdall_gateway_queue_diagnostics {
	atomic_t allocation_failures;
	atomic_t depth;
	atomic_t depth_high_water;
};

struct heimdall_runtime_counters {
	uint32_t rx_frames;
	uint32_t rx_validated;
	uint32_t rx_errors;
	uint32_t rx_fcs_errors;
	uint32_t rx_filter_rejects;
	uint32_t reject_length;
	uint32_t reject_header;
	uint32_t reject_config;
	uint32_t reject_schedule;
	uint32_t reject_stale;
	uint32_t reject_subreport;
	uint32_t subreport_crc_failures;
	uint32_t diagnostic_failures;
	uint32_t cir_reads;
	uint32_t subreport_encode_failures;
	uint32_t report_assembly_failures;
	uint32_t tx_attempted;
	uint32_t tx_completed;
	uint32_t tx_start_late;
	uint32_t tx_timestamp_errors;
	uint32_t bootstrap_transmissions;
	uint32_t peer_adoptions;
	uint32_t watchdog_transmissions;
	uint32_t identity_collisions;
	uint32_t configuration_inhibitions;
	uint32_t tx_timeout_recoveries;
	uint32_t callback_max_us;
	uint32_t last_rx_k;
	uint32_t last_tx_k;
	uint64_t last_rx_timestamp;
	uint64_t last_programmed_tx_timestamp;
	int64_t last_tx_error_dtu;
	uint64_t first_tx_system_timestamp;
	uint64_t first_programmed_tx_timestamp;
	int64_t first_tx_lead_dtu;
	uint8_t synchronized;
	uint8_t tx_armed;
	uint8_t evidence_age;
	uint8_t reserved;
};

extern volatile struct heimdall_runtime_counters heimdall_runtime_counters;
extern struct heimdall_gateway_queue_diagnostics
	heimdall_gateway_queue_diagnostics;

int heimdall_beacon_runtime_run(void);
