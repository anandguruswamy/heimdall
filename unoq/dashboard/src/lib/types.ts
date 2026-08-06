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
  'Radar Map',
  'Training',
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

// Seat-classifier class names; must match the training scripts exactly
// (FrontLeft=0, FrontRight=1, BackRight=2, BackLeft=3, Empty=4). The UI may display
// "Rear Left"/"Rear Right" but always sends these identifiers.
export const seatClasses = ['FrontLeft', 'FrontRight', 'BackRight', 'BackLeft', 'Empty'] as const;
export type SeatClass = (typeof seatClasses)[number];

export type TrainingVariant = 'raw' | 'calibrated';
export type TrainingMode = 'seat' | 'person' | 'separate' | 'joint';
export type TrainingArchitecture = 'standard' | 'lite';
export type TrainingLinkMode = 'canonical' | 'directed';
export type StartTrainingPayload = {
  variant: TrainingVariant;
  epochs: number;
  mode: TrainingMode;
  architecture: TrainingArchitecture;
  link_mode: TrainingLinkMode;
  taps_left: number;
  taps_right: number;
  patience: number;
};

export type TopicKey = 'health' | 'distance' | 'cir' | 'waterfall' | 'slow-fft' | 'fast-fft' | 'cfo' | 'calibration' | 'seat-inference';
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
