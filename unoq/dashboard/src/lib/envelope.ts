import type { Envelope, TopicKey } from './types';

const topics: TopicKey[] = ['health', 'distance', 'cir', 'waterfall', 'slow-fft', 'fast-fft', 'cfo', 'calibration', 'seat-inference'];
const utf8 = new TextDecoder('utf-8', { fatal: true });

export function decodeEnvelope(buffer: ArrayBuffer, byteOffset = 0, byteLength = buffer.byteLength - byteOffset): Envelope {
  const bytes = new Uint8Array(buffer, byteOffset, byteLength);
  const view = new DataView(buffer, byteOffset, byteLength);
  const requireRange = (offset: number, length: number) => {
    if (!Number.isSafeInteger(offset) || offset < 0 || offset + length > bytes.length) throw new Error('Truncated HMT1 envelope');
  };
  const u16 = (offset: number) => { requireRange(offset, 2); return view.getUint16(offset, true); };
  const u32 = (offset: number) => { requireRange(offset, 4); return view.getUint32(offset, true); };
  const u64 = (offset: number) => { requireRange(offset, 8); return view.getBigUint64(offset, true); };

  requireRange(0, 8);
  if (String.fromCharCode(...bytes.subarray(4, 8)) !== 'HMT1') throw new Error('Invalid telemetry identifier');
  const table = u32(0);
  requireRange(table, 4);
  const vtableDistance = view.getInt32(table, true);
  if (vtableDistance <= 0 || vtableDistance > table) throw new Error('Invalid HMT1 vtable');
  const vtable = table - vtableDistance;
  const vtableLength = u16(vtable);
  requireRange(vtable, vtableLength);
  const field = (index: number) => {
    const entry = vtable + 4 + index * 2;
    return entry + 2 <= vtable + vtableLength ? u16(entry) : 0;
  };
  const scalar = (index: number) => { const offset = field(index); return offset ? table + offset : -1; };
  const topicField = scalar(1);
  if (topicField >= 0) requireRange(topicField, 1);
  const topicNumber = topicField < 0 ? 0 : bytes[topicField];
  if (!topics[topicNumber]) throw new Error(`Unknown HMT1 topic ${topicNumber}`);
  const payloadField = scalar(6);
  if (payloadField < 0) throw new Error('HMT1 payload missing');
  const vector = payloadField + u32(payloadField);
  const payloadLength = u32(vector);
  requireRange(vector + 4, payloadLength);
  const text = utf8.decode(bytes.subarray(vector + 4, vector + 4 + payloadLength));

  return {
    schemaVersion: scalar(0) < 0 ? 1 : u16(scalar(0)),
    topic: topics[topicNumber],
    streamSequence: scalar(2) < 0 ? 0n : u64(scalar(2)),
    configurationEpoch: scalar(3) < 0 ? 0n : u64(scalar(3)),
    processingEpoch: scalar(4) < 0 ? 0n : u64(scalar(4)),
    droppedEvents: scalar(5) < 0 ? 0 : u32(scalar(5)),
    payload: JSON.parse(text) as unknown
  };
}

export function decodeEnvelopes(buffer: ArrayBuffer): Envelope[] {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 4 || String.fromCharCode(...bytes.subarray(0, 4)) !== 'HMB1') return [decodeEnvelope(buffer)];
  if (bytes.length < 8) throw new Error('Truncated HMB1 batch');
  const view = new DataView(buffer), count = view.getUint16(4, true), envelopes: Envelope[] = [];
  let offset = 8;
  for (let index = 0; index < count; index++) {
    if (offset + 4 > bytes.length) throw new Error('Truncated HMB1 item length');
    const length = view.getUint32(offset, true); offset += 4;
    if (offset + length > bytes.length) throw new Error('Truncated HMB1 item');
    envelopes.push(decodeEnvelope(buffer, offset, length));
    offset += length;
  }
  if (offset !== bytes.length) throw new Error('Trailing HMB1 data');
  return envelopes;
}
