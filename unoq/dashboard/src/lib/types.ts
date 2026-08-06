export const tabs = [
  'Network Health',
  'Live Distance',
  'Board Positions',
  'Live CIR',
  'CIR Waterfall',
  'Slow-Time FFT',
  'Fast-Time FFT',
  'CFO',
  'Distance Calibration',
  'Simulator'
] as const;

export type Tab = (typeof tabs)[number];
export type Link = { from: number; to: number; id: string };
export type PositionRange = { a: number; b: number; raw?: number; smoothed?: number; ultra?: number; outlier: boolean; eventS: number; round: number; evidence: string; window: number[] };
export type Series = { data: Float32Array; color: string; width?: number; points?: boolean; pointSize?: number; ranging?: 'ss'|'ds'; smoothed?: boolean };
export type Marker = { at: number; color: string };
export type PlotFrame = {
  series?: Series[];
  heatmap?: Float32Array;
  heatWidth?: number;
  heatHeight?: number;
  min?: number;
  max?: number;
  markers?: Marker[];
  xLabel?: string;
  yLabel?: string;
};

export type StreamStatus = 'live' | 'connecting' | 'offline';

export const seatIds = ['front_left', 'front_right', 'rear_left', 'rear_right'] as const;
export type SeatId = (typeof seatIds)[number];
// timestamp: epoch ms of the backend update that produced this state, not delivery time.
export type SeatState = { seats: Record<SeatId, boolean>; timestamp: number };

export type TopicKey = 'health' | 'distance' | 'cir' | 'waterfall' | 'slow-fft' | 'fast-fft' | 'cfo' | 'calibration';
export type Envelope = {
  schemaVersion: number;
  topic: TopicKey;
  streamSequence: bigint;
  configurationEpoch: bigint;
  processingEpoch: bigint;
  droppedEvents: number;
  receivedAtMs?: number;
  payload: unknown;
};

export type LinkLiveData = Partial<Record<TopicKey, PlotFrame>> & {
  fastFftPhase?: PlotFrame;
  updatedAt?: number;
  distanceCm?: number;
  quality?: number;
  cfoPpm?: number;
  payloads?: Partial<Record<TopicKey, Record<string, unknown>>>;
};
