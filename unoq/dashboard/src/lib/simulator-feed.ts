import { seatIds, type SeatId, type SeatState } from './types';

export type SeatListener = (state: SeatState) => void;

// The Simulator tab consumes this interface only; the real backend client can
// replace MockSeatFeed by implementing subscribe() the same way api.ts exposes
// its WebSocket topics.
export interface SeatFeed {
  subscribe(listener: SeatListener): () => void;
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
