# Protocol Rules

Heimdall has two intentionally separate protocols:

1. UWB beacon protocol between radio nodes.
2. USB CDC export protocol between the gateway node and UNO Q.

Both are versioned independently. The UNO Q must be able to reject an unknown
version without crashing or silently interpreting fields incorrectly.

## Global rules

- Little-endian integer encoding unless a contract says otherwise.
- Every frame has a magic value, protocol version, length, sequence, and CRC.
- Every observation carries `node_id`, `round_id`, and a source sequence.
- Radio timestamps retain their native device-clock units until conversion.
- Missing data is explicit; zero is never used as a missing-value sentinel.
- Firmware queues are bounded. Backpressure must not block radio timing.
- Duplicate records are safe to replay and detectable by identity plus sequence.
