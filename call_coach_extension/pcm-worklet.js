// Turns the 2-channel (rep mic, customer tab) audio into interleaved
// 16-bit PCM chunks of ~100 ms for Deepgram's multichannel stream.
class PcmWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunkFrames = Math.round(sampleRate / 10);
    this.buffer = new Int16Array(this.chunkFrames * 2);
    this.filled = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const left = input[0];
    const right = input[1] || input[0];
    for (let i = 0; i < left.length; i++) {
      const l = Math.max(-1, Math.min(1, left[i]));
      const r = Math.max(-1, Math.min(1, right[i]));
      this.buffer[this.filled * 2] = l < 0 ? l * 0x8000 : l * 0x7fff;
      this.buffer[this.filled * 2 + 1] = r < 0 ? r * 0x8000 : r * 0x7fff;
      this.filled++;
      if (this.filled === this.chunkFrames) {
        this.port.postMessage(this.buffer.buffer, [this.buffer.buffer]);
        this.buffer = new Int16Array(this.chunkFrames * 2);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PcmWorklet);
