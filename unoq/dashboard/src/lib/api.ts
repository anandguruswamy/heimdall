import { decodeEnvelopes } from './envelope';
import type { Envelope, StreamStatus, Tab } from './types';

export type BootstrapData = { health?: unknown; topology?: unknown; distanceHistory?: unknown; settings?: unknown; calibration?: unknown };

export class HeimdallApi {
  private socket?: WebSocket;
  private retry?: ReturnType<typeof setTimeout>;
  private stopped = false;
  private topic = '';
  private attempt = 0;

  constructor(
    private readonly base = '/api',
    private readonly onStatus: (status: StreamStatus) => void,
    private readonly onFrame: (envelope: Envelope) => void,
    private readonly onBootstrap: (data: BootstrapData) => void,
    private readonly onError: (message: string) => void,
    private readonly onReset: () => void
  ) {}

  async get<T>(path: string): Promise<T> {
    const response = await fetch(`${this.base}${path}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json() as Promise<T>;
  }

  async send<T>(path: string, method: 'POST' | 'PUT', body: unknown): Promise<T> {
    const response = await fetch(`${this.base}${path}`, { method, headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(`${method} ${path}: HTTP ${response.status}`);
    return response.json() as Promise<T>;
  }

  putSettings(value: unknown): Promise<unknown> { return this.send('/settings', 'PUT', value); }
  getCalibration(): Promise<unknown> { return this.get('/calibration'); }
  snapshotCalibration(): Promise<unknown> { return this.send('/calibration/snapshot', 'POST', {}); }
  solveCalibration(referencesM: Record<string, number>): Promise<unknown> { return this.send('/calibration/solve', 'POST', { references_m: referencesM }); }
  applyCalibration(): Promise<unknown> { return this.send('/calibration/apply', 'POST', {}); }
  rollbackCalibration(): Promise<unknown> { return this.send('/calibration/rollback', 'POST', {}); }
  saveClip(name = '', note = ''): Promise<unknown> { return this.send('/clips', 'POST', { name, note }); }
  getClips(): Promise<unknown> { return this.get('/clips'); }
  clipDownload(id: number): string { return `${this.base}/clips/${id}`; }
  async deleteClip(id: number): Promise<void> {
    const response=await fetch(`${this.base}/clips/${id}`,{method:'DELETE'});
    if (!response.ok && response.status !== 404) throw new Error(`DELETE /clips/${id}: HTTP ${response.status}`);
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  subscribe(tab: Tab): void {
    this.topic = tab === 'Live CIR' ? 'instantaneous-cir' : tab === 'Board Positions' ? 'live-distance' : tab.toLowerCase().replaceAll(' ', '-');
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.sendSubscription();
      if (this.topic === 'live-distance') void this.bootstrapDistanceHistory();
    }
  }

  close(): void {
    this.stopped = true;
    clearTimeout(this.retry);
    this.socket?.close();
  }

  private open(): void {
    this.onStatus('connecting');
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = new WebSocket(`${protocol}//${location.host}${this.base}/stream`);
    this.socket.binaryType = 'arraybuffer';
    this.socket.onopen = () => {
      this.onReset();
      this.attempt = 0;
      this.onStatus('live');
      this.sendSubscription();
      void this.bootstrap();
      if (this.topic === 'live-distance') void this.bootstrapDistanceHistory();
    };
    this.socket.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      try { const receivedAtMs=performance.now(); for(const envelope of decodeEnvelopes(event.data)){envelope.receivedAtMs=receivedAtMs;this.onFrame(envelope);} }
      catch (error) { this.onError(error instanceof Error ? error.message : 'Invalid telemetry envelope'); }
    };
    this.socket.onerror = () => this.socket?.close();
    this.socket.onclose = () => {
      if (this.stopped) return;
      this.onStatus('offline');
      const delay = Math.min(15_000, 750 * 2 ** this.attempt++);
      this.retry = setTimeout(() => this.open(), delay + Math.random() * 300);
    };
  }

  private sendSubscription(): void {
    this.socket?.send(JSON.stringify({ op: 'subscribe', topics: [this.topic] }));
  }

  private async bootstrap(): Promise<void> {
    const paths = [['health','health'],['topology','topology'],['settings','settings'],['calibration','calibration']] as const;
    const results = await Promise.allSettled(paths.map(([,path]) => this.get(`/${path}`)));
    const data: BootstrapData = {};
    results.forEach((result, index) => { if (result.status === 'fulfilled') data[paths[index][0]] = result.value; });
    const failures = results.filter((result) => result.status === 'rejected');
    if (failures.length) this.onError(`${failures.length} backend bootstrap request${failures.length === 1 ? '' : 's'} failed`);
    this.onBootstrap(data);
  }

  private async bootstrapDistanceHistory(): Promise<void> {
    try { this.onBootstrap({distanceHistory:await this.get('/history/distance')}); }
    catch { this.onError('Distance history resynchronization failed'); }
  }
}
