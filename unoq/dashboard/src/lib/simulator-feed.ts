import { seatIds, type SeatId, type SeatState } from './types';

export type SeatListener = (state: SeatState) => void;

// The Simulator tab consumes this interface only; the real backend client can
// replace MockSeatFeed by implementing subscribe() the same way api.ts exposes
// its WebSocket topics.
export interface SeatFeed {
  subscribe(listener: SeatListener): () => void;
}

// Classifier class index -> Simulator seat id. Class order comes from the
// training scripts: FrontLeft=0, FrontRight=1, BackRight=2, BackLeft=3.
export const classSeatIds: readonly SeatId[] = ['front_left', 'front_right', 'rear_right', 'rear_left'];

export type SeatPrediction = { seat: string; seatIndex: number; probs: number[]; frameId: number | null; ts: number };
export type LiveInfo = { latest: SeatPrediction | null; stable: SeatId | null; uncertain: boolean };

// SeatFeed driven by seat-inference WebSocket predictions. The 4-class model
// has no "empty" class and always names exactly one seat, so the emitted
// SeatState marks a single occupant. Two stabilizers prevent the 3D occupant
// from flickering: a majority vote over the last `windowSize` confident
// predictions, and a confidence threshold below which the prediction is
// marked uncertain and the last stable seat is kept.
export class LiveSeatFeed implements SeatFeed {
  windowSize = 5;
  confidenceThreshold = 0.6;
  private listeners = new Set<SeatListener>();
  private infoListeners = new Set<(info: LiveInfo) => void>();
  private votes: number[] = [];
  private latest: SeatPrediction | null = null;
  private stableIndex: number | null = null;
  private uncertain = false;
  private lastTs = Date.now();

  subscribe(listener: SeatListener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    return () => { this.listeners.delete(listener); };
  }

  onInfo(listener: (info: LiveInfo) => void): () => void {
    this.infoListeners.add(listener);
    listener(this.info());
    return () => { this.infoListeners.delete(listener); };
  }

  push(payload: Record<string, unknown>): void {
    const probs = Array.isArray(payload.probs) ? (payload.probs as unknown[]).map(Number) : null;
    const seatIndex = Number(payload.seat_index);
    if (!probs || probs.length !== classSeatIds.length || probs.some(Number.isNaN)) return;
    if (!Number.isInteger(seatIndex) || seatIndex < 0 || seatIndex >= classSeatIds.length) return;
    const ts = Number(payload.ts);
    this.lastTs = Number.isFinite(ts) && ts > 0 ? ts : Date.now();
    this.latest = {
      seat: String(payload.seat ?? ''),
      seatIndex,
      probs,
      frameId: payload.frame_id == null ? null : Number(payload.frame_id),
      ts: this.lastTs
    };
    if (probs[seatIndex] >= this.confidenceThreshold) {
      this.uncertain = false;
      this.votes.push(seatIndex);
      if (this.votes.length > this.windowSize) this.votes.shift();
      const counts = classSeatIds.map((_, index) => this.votes.filter((vote) => vote === index).length);
      const winner = counts.indexOf(Math.max(...counts));
      if (counts[winner] > this.votes.length / 2 && winner !== this.stableIndex) {
        this.stableIndex = winner;
        this.emitSeats();
      }
    } else {
      this.uncertain = true; // keep the last stable seat
    }
    this.emitInfo();
  }

  reset(): void {
    this.votes = [];
    this.latest = null;
    this.stableIndex = null;
    this.uncertain = false;
    this.lastTs = Date.now();
    this.emitSeats();
    this.emitInfo();
  }

  info(): LiveInfo {
    return {
      latest: this.latest,
      stable: this.stableIndex === null ? null : classSeatIds[this.stableIndex],
      uncertain: this.uncertain
    };
  }

  private snapshot(): SeatState {
    const seats = Object.fromEntries(seatIds.map((id) => [id, false])) as Record<SeatId, boolean>;
    if (this.stableIndex !== null) seats[classSeatIds[this.stableIndex]] = true;
    return { seats, timestamp: this.lastTs };
  }

  private emitSeats(): void {
    const state = this.snapshot();
    for (const listener of this.listeners) listener(state);
  }

  private emitInfo(): void {
    const info = this.info();
    for (const listener of this.infoListeners) listener(info);
  }
}

export class MockSeatFeed implements SeatFeed {
  private seats: Record<SeatId, boolean> = { front_left: true, front_right: false, rear_left: false, rear_right: true };
  private listeners = new Set<SeatListener>();
  private timer: ReturnType<typeof setInterval> | undefined;
  private autoMode = true;
  private lastUpdate = Date.now();

  subscribe(listener: SeatListener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    this.syncTimer();
    return () => {
      this.listeners.delete(listener);
      this.syncTimer();
    };
  }

  get auto(): boolean { return this.autoMode; }

  setAuto(enabled: boolean): void {
    this.autoMode = enabled;
    this.syncTimer();
    if (enabled) this.randomize();
  }

  setSeat(id: SeatId, occupied: boolean): void {
    this.autoMode = false;
    this.syncTimer();
    if (this.seats[id] === occupied) { this.emit(); return; }
    this.seats = { ...this.seats, [id]: occupied };
    this.lastUpdate = Date.now();
    this.emit();
  }

  private snapshot(): SeatState {
    return { seats: { ...this.seats }, timestamp: this.lastUpdate };
  }

  private emit(): void {
    const state = this.snapshot();
    for (const listener of this.listeners) listener(state);
  }

  private randomize(): void {
    // Weighted occupant count keeps multi-seat states common: driver-only trips
    // exist, but so do full cars.
    const counts = [0, 1, 1, 2, 2, 2, 3, 3, 4];
    const target = counts[Math.floor(Math.random() * counts.length)];
    const shuffled = [...seatIds].sort(() => Math.random() - 0.5);
    const occupied = new Set(shuffled.slice(0, target));
    if (target > 0 && Math.random() < 0.7) occupied.add('front_left');
    this.seats = Object.fromEntries(seatIds.map((id) => [id, occupied.has(id)])) as Record<SeatId, boolean>;
    this.lastUpdate = Date.now();
    this.emit();
  }

  private syncTimer(): void {
    const shouldRun = this.autoMode && this.listeners.size > 0;
    if (shouldRun && this.timer === undefined) this.timer = setInterval(() => this.randomize(), 3500);
    if (!shouldRun && this.timer !== undefined) { clearInterval(this.timer); this.timer = undefined; }
  }
}
