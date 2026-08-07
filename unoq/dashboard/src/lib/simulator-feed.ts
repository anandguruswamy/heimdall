import { seatClasses, seatIds, type SeatClass, type SeatId, type SeatState } from './types';

export type SeatListener = (state: SeatState) => void;

// The Presence Detection tab consumes this interface only; the real backend client can
// replace MockSeatFeed by implementing subscribe() the same way api.ts exposes
// its WebSocket topics.
export interface SeatFeed {
  subscribe(listener: SeatListener): () => void;
}

// Empty has no physical seat and therefore maps to null.
export const classSeatIds: readonly (SeatId | null)[] = ['front_left', 'front_right', 'rear_right', 'rear_left', null];

export type SeatPrediction = { seat: SeatClass; seatIndex: number; probs: number[]; person: string | null; frameId: number | null; ts: number };
// Multilabel checkpoints: four independent per-seat probabilities in bit
// order FrontLeft, FrontRight, BackRight, BackLeft (they do not sum to 1).
export type SeatBits = { probs: number[]; occupied: boolean[]; uncertainBits: boolean[]; threshold: number };
export type LiveInfo = {
  latest: SeatPrediction | null;
  raw: SeatPrediction | null;
  stablePrediction: SeatPrediction | null;
  stable: SeatId | null;
  stableClass: SeatClass | null;
  person: string | null;
  uncertain: boolean;
  bits: SeatBits | null;
  mode: 'single' | 'multilabel';
};

// Stable backend predictions are authoritative. The majority vote remains only
// for old or raw-only backends so smoothing is never applied twice.
export class LiveSeatFeed implements SeatFeed {
  windowSize = 5;
  confidenceThreshold = 0.6;
  private listeners = new Set<SeatListener>();
  private infoListeners = new Set<(info: LiveInfo) => void>();
  private votes: number[] = [];
  private latest: SeatPrediction | null = null;
  private raw: SeatPrediction | null = null;
  private stablePrediction: SeatPrediction | null = null;
  private stableIndex: number | null = null;
  private person: string | null = null;
  private uncertain = false;
  private bits: SeatBits | null = null;
  private mode: 'single' | 'multilabel' = 'single';
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
    const ts = Number(payload.ts);
    this.lastTs = Number.isFinite(ts) && ts > 0 ? ts : Date.now();
    if (Object.hasOwn(payload, 'stable_seat_bits') || Object.hasOwn(payload, 'raw_seat_bits')) {
      this.pushBits(payload);
      return;
    }
    // A single-label payload ends any multilabel run (e.g. another client
    // swapped checkpoints) so stale bits never keep rendering.
    this.mode = 'single';
    this.bits = null;
    const hasStable = Object.hasOwn(payload, 'stable_seat') || Object.hasOwn(payload, 'stable_seat_probs');
    this.raw = this.parsePrediction(payload.raw_seat ?? payload.seat, payload.raw_seat_probs ?? payload.probs, payload.raw_person ?? payload.person, payload);

    if (!hasStable && !this.raw && (Object.hasOwn(payload, 'stable_person') || Object.hasOwn(payload, 'raw_person') || Object.hasOwn(payload, 'person'))) {
      const probabilities = this.probabilities(payload.stable_person_probs ?? payload.raw_person_probs);
      this.person = this.personName(payload.stable_person ?? payload.raw_person ?? payload.person);
      this.latest = null;
      this.stablePrediction = null;
      this.stableIndex = null;
      this.votes = [];
      this.uncertain = probabilities ? Math.max(...probabilities) < this.confidenceThreshold : false;
      this.emitSeats();
      this.emitInfo();
      return;
    }

    if (hasStable) {
      const stable = this.parsePrediction(payload.stable_seat, payload.stable_seat_probs, payload.stable_person ?? payload.person, payload);
      if (!stable) return;
      this.stablePrediction = stable;
      this.latest = stable;
      this.stableIndex = stable.seatIndex;
      this.person = stable.seat === 'Empty' ? null : stable.person;
      this.uncertain = typeof payload.uncertain === 'boolean' ? payload.uncertain : stable.probs[stable.seatIndex] < this.confidenceThreshold;
      this.votes = [];
      this.emitSeats();
      this.emitInfo();
      return;
    }

    const prediction = this.raw;
    if (!prediction) return;
    this.stablePrediction = null;
    this.latest = prediction;
    this.person = prediction.seat === 'Empty' ? null : prediction.person;
    if (prediction.probs[prediction.seatIndex] >= this.confidenceThreshold) {
      this.uncertain = false;
      this.votes.push(prediction.seatIndex);
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

  // Multilabel payloads: the backend smoother already averaged the bits and
  // applied the threshold; every occupied bit lights its seat independently
  // (including none of them for an empty cabin).
  private pushBits(payload: Record<string, unknown>): void {
    const probs = this.probabilities(payload.stable_seat_bits ?? payload.raw_seat_bits);
    if (!probs || probs.length !== 4) return;
    const rawThreshold = Number(payload.threshold);
    const threshold = Number.isFinite(rawThreshold) && rawThreshold > 0 && rawThreshold < 1 ? rawThreshold : 0.5;
    const occupied = this.booleans(payload.stable_seat_occupied ?? payload.raw_seat_occupied)
      ?? probs.map((value) => value >= threshold);
    const uncertainBits = this.booleans(payload.uncertain_bits)
      ?? probs.map((value) => Math.abs(value - threshold) < 0.1);
    this.mode = 'multilabel';
    this.bits = { probs, occupied, uncertainBits, threshold };
    this.latest = null;
    this.raw = null;
    this.stablePrediction = null;
    this.stableIndex = null;
    this.votes = [];
    this.person = null;
    this.uncertain = payload.uncertain === true;
    this.emitSeats();
    this.emitInfo();
  }

  reset(): void {
    this.votes = [];
    this.latest = null;
    this.raw = null;
    this.stablePrediction = null;
    this.stableIndex = null;
    this.person = null;
    this.uncertain = false;
    this.bits = null;
    this.mode = 'single';
    this.lastTs = Date.now();
    this.emitSeats();
    this.emitInfo();
  }

  info(): LiveInfo {
    return {
      latest: this.latest,
      raw: this.raw,
      stablePrediction: this.stablePrediction,
      stable: this.stableIndex === null ? null : classSeatIds[this.stableIndex],
      stableClass: this.stableIndex === null ? null : seatClasses[this.stableIndex],
      person: this.person,
      uncertain: this.uncertain,
      bits: this.bits,
      mode: this.mode
    };
  }

  private parsePrediction(seatValue: unknown, probsValue: unknown, personValue: unknown, payload: Record<string, unknown>): SeatPrediction | null {
    const probs = this.probabilities(probsValue);
    if (!probs || (probs.length !== 4 && probs.length !== seatClasses.length) || probs.some((value) => !Number.isFinite(value))) return null;
    const namedIndex = seatClasses.findIndex((seat) => seat === seatValue);
    const valueIndex = Number(seatValue);
    const legacyIndex = Number(payload.seat_index);
    const seatIndex = namedIndex >= 0 ? namedIndex : Number.isInteger(valueIndex) ? valueIndex : legacyIndex;
    if (!Number.isInteger(seatIndex) || seatIndex < 0 || seatIndex >= probs.length) return null;
    const person = this.personName(personValue);
    return {
      seat: seatClasses[seatIndex],
      seatIndex,
      probs,
      person,
      frameId: payload.frame_id == null ? null : Number(payload.frame_id),
      ts: this.lastTs
    };
  }

  private probabilities(value: unknown): number[] | null {
    const probabilities = Array.isArray(value) ? value.map(Number) : null;
    return probabilities && probabilities.length > 0 && probabilities.every(Number.isFinite) ? probabilities : null;
  }

  private booleans(value: unknown): boolean[] | null {
    return Array.isArray(value) && value.length === 4 && value.every((item) => typeof item === 'boolean')
      ? (value as boolean[])
      : null;
  }

  private personName(value: unknown): string | null {
    if (typeof value !== 'string' || !value.trim() || value.trim().toLowerCase() === 'n/a') return null;
    return value.trim();
  }

  private snapshot(): SeatState {
    const seats = Object.fromEntries(seatIds.map((id) => [id, false])) as Record<SeatId, boolean>;
    if (this.mode === 'multilabel') {
      this.bits?.occupied.forEach((occupied, index) => {
        const seat = classSeatIds[index];
        if (occupied && seat) seats[seat] = true;
      });
      return { seats, timestamp: this.lastTs };
    }
    const stableSeat = this.stableIndex === null ? null : classSeatIds[this.stableIndex];
    if (stableSeat) seats[stableSeat] = true;
    const person = this.latest?.person?.trim();
    const people = stableSeat && person && person.toLowerCase() !== 'n/a'
      ? { [stableSeat]: person }
      : undefined;
    return { seats, people, timestamp: this.lastTs };
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
