// AudioWorklet: plays interleaved stereo Float32 chunks from the audio worker through a ring buffer.
// Starts after ~200 ms is buffered; outputs silence on underrun; drops the oldest audio past 1.5 s.
class RfscanPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.cap = sampleRate * 4;                 // frames
    this.L = new Float32Array(this.cap); this.R = new Float32Array(this.cap);
    this.w = 0; this.r = 0; this.fill = 0; this.primed = false; this.gain = 1; this.underruns = 0; this.n = 0;
    this.port.onmessage = e => {
      const m = e.data;
      if (m.gain !== undefined) { this.gain = m.gain; return; }
      if (m.reset) { this.w = this.r = this.fill = 0; this.primed = false; return; }
      const a = new Float32Array(m.audio), n = a.length >> 1;
      if (this.fill + n > sampleRate * 1.5) { const drop = this.fill + n - sampleRate; this.r = (this.r + drop) % this.cap; this.fill -= drop; }
      for (let k = 0; k < n; k++) { this.L[this.w] = a[2 * k]; this.R[this.w] = a[2 * k + 1]; this.w = (this.w + 1) % this.cap; }
      this.fill += n;
      if (!this.primed && this.fill >= sampleRate * 0.2) this.primed = true;
    };
  }
  process(inputs, outputs) {
    const out = outputs[0], L = out[0], R = out[1] || out[0], n = L.length;
    if (!this.primed || this.fill < n) {
      L.fill(0); R.fill(0);
      if (this.primed) { this.underruns++; this.primed = false; }
    } else {
      const g = this.gain;
      for (let k = 0; k < n; k++) { L[k] = this.L[this.r] * g; R[k] = this.R[this.r] * g; this.r = (this.r + 1) % this.cap; }
      this.fill -= n;
    }
    if (++this.n % 40 === 0) this.port.postMessage({ fill: this.fill / sampleRate, underruns: this.underruns });
    return true;
  }
}
registerProcessor('rfscan-player', RfscanPlayer);
