export const tabs = [
  'Network Health',
  'Live Distance',
  'Instantaneous CIR',
  'CIR Waterfall',
  'Slow-Time FFT',
  'Fast-Time FFT',
  'CFO',
  'Distance Calibration'
] as const;

export type Tab = (typeof tabs)[number];
export type Link = { from: number; to: number; id: string };
export type Series = { data: Float32Array; color: string; width?: number; points?: boolean };
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

export type TopicKey = 'health' | 'distance' | 'cir' | 'waterfall' | 'slow-fft' | 'fast-fft' | 'cfo' | 'calibration';
export type Envelope = {
  schemaVersion: number;
  topic: TopicKey;
  streamSequence: bigint;
  configurationEpoch: bigint;
  processingEpoch: bigint;
  droppedEvents: number;
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
