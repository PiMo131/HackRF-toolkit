# Live audio listening in the browser version — research notes (2026-10-28)

Web research only. Context: Chrome WebUSB, HackRF 8-bit IQ, JS DSP in Web Workers, Pyodide decoders,
multimon-ng + rtl_433 as WebAssembly, static GitHub Pages hosting (no custom response headers).
Items marked **[unverified]** could not be confirmed from a primary source.

## 0. Two facts that shape everything

**HackRF sample-rate floor.** Sampling below 8 Msps is "not recommended": the MAX2837 baseband filter
has a 1.75 MHz minimum, so at 2 Msps adjacent energy aliases in. Capture at 8 Msps and decimate in
software (https://hackrf.readthedocs.io/en/latest/sampling_rate.html). So every audio mode means
8 Msps × 8-bit IQ = 16 MB/s over WebUSB and a worker-side decimator. The first stage
(8 M → 240–500 kHz) is the one CPU-heavy item; do it as a polyphase/half-band cascade.

**Audio output from a worker.** AudioContext/AudioWorklet are main-thread only. Working pattern:
worker → `postMessage(Float32Array, [buffer])` → main thread → `AudioWorkletNode.port` → ring buffer
in the worklet, with a 150–250 ms jitter buffer (this is what no-sdr does). A SharedArrayBuffer ring
needs cross-origin isolation, which GitHub Pages cannot set headers for (workaround: coi-serviceworker
shim). Start with transferable postMessage. End-to-end browser audio latency is ~55–70 ms; a
100–200 ms buffer is normal for radio.

## 1. WBFM broadcast with stereo + RDS — EASY

Prior art, all browser-side:
* **Radio Receiver** (Google, archived, Apache-2.0, https://github.com/google/radioreceiver) rewritten
  by Jacobo Tarrío: app https://github.com/jtarrio/radioreceiver, WebUSB driver `@jtarrio/webrtlsdr`,
  DSP `@jtarrio/signals` (Apache-2.0). RTL-only driver; demod library is hardware-agnostic. No RDS.
* **no-sdr** (MIT, https://github.com/gbozo/no-sdr): the client is pure TypeScript and the most
  reusable reference. `client/src/engine/demodulators.ts` (FM discriminator → 240 kHz MPX, 19 kHz
  pilot PLL, stereo blend, 75 µs de-emphasis, 240 k → 48 k), `rds-decoder.ts` (57 kHz BPF, 2375 baud
  symbol sync, 26-bit block sync, groups 0A/2A/4A: PS, RT, CT, AF, PTY/TP/TA), `audio.ts`
  (AudioWorklet ring buffer). Published figures: FM demod ~38× real time in JS; RDS < 0.5 % CPU.
* **BrowSDR** (AGPL-3.0, https://github.com/jLynx/BrowSDR): HackRF One/Pro over WebUSB, WFM/NFM/AM/SSB,
  RDS, Rust→WASM, multiple VFOs with squelch. Closest analogue to our app, but AGPL: copying code
  would re-licence the project.
* **rad.io** (https://github.com/alexthemitchell/rad.io, HackRF-only, Rust→WASM, no licence file).
* Standalone RDS libraries are native only (redsea C++ MIT, librdsparser C, rds-rs Rust).

Rates: 8 Msps → 240–256 kHz → discriminator → MPX → 48 kHz. About 3 % of one core in plain JS.
Recommended: port no-sdr's three TS files (MIT) into the worker; 1–2 days for stereo + RDS.

## 2. DAB+ — HARD

No browser/WASM DAB decoder exists. welle.io (GPL, Qt, libfaad; its "-w" web interface is a
server-side front-end), dablin (ETI/EDI input only), qt-dab (GPL) all native.
**williamyang98/DAB-Radio** (MIT, C++, https://github.com/williamyang98/DAB-Radio) is the most
portable candidate: clean `src/ofdm` / `src/dab` split, CMake, minimal deps, a realistic Emscripten
target. Its README lists DAB+ superframe RS correction as unfinished.

Audio codec is the key constraint: DAB+ is HE-AAC with **960-sample frames**.
* WebCodecs: branded Chrome maps `mp4a.40.5/29` to AAC, but Chromium's FFmpeg historically rejects
  SBR with 960-frame length (https://trac.ffmpeg.org/ticket/1407; DAB+ patch May 2023; a 2026-04
  commit "aacdec: add support for 960-frame HE-AAC" seen only on a mirror **[unverified]**).
  Do not rely on it; treat as an optional fast path after a runtime test.
* WASM: FAAD2 (GPL-2.0+, patent warning) handles DAB+; existing builds `@ohrstrom/faad2-wasm`,
  `aac-decode`. fdk-aac WASM only found as an encoder.

CPU: 2.048 Msps (from 8 Msps, 4:1), 2048-pt FFT per symbol, rate-¼ Viterbi, RS, HE-AAC.
Estimate 30–60 % of one core in WASM **[not measured]**. Budget: weeks. Do last.

## 3. Live P2000 / FLEX feed — EASY-MEDIUM

It is data, not voice: NFM demod of a 12.5 kHz channel → 22 050 Hz s16 audio → FLEX decoder →
message stream (capcode, time, text) → scrolling list with filters (brandweer/ambulance/politie),
optional sound/TTS.

multimon-ng `demod_flex.c` (GPL-3+): fixed 22 050 Hz, 1600/3200 bps 2/4-FSK, `flex_demod()` is
called per buffer with state persisting across buffers, i.e. already streaming-safe. Canonical:
`rtl_fm -f 169.65M -s 22050 | multimon-ng -t raw -a FLEX -`.

Streaming in our WASM build, two ways:
1. keep `main()` and feed stdin through Emscripten `FS.init(input, …)` (byte-at-a-time, hacky);
2. **preferred:** bypass `main()`; export `flex_init`, `flex_demod` and route `verbprintf` to a JS
   callback; call `flex_demod(buf, n)` per 22.05 kHz chunk. ~1 day of C glue.

No JS/TS/WASM FLEX decoders exist. Legal (NL): in 2013 the minister announced intent to stop public
following of P2000 (https://zoek.officielebekendmakingen.nl/ah-tk-20122013-2762.pdf); the 2018 network
renewal "allows encrypted messages" (https://zoek.officielebekendmakingen.nl/kst-25124-77.html);
most traffic is still clear **[unverified for 2026]**.

## 4. Narrowband voice scanner (PMR446, marine, airband AM, 2 m / 70 cm) — EASY-MEDIUM

Building blocks: channelizer on the wideband capture (8 Msps covers all 16 PMR446 channels, all
marine channels, most of airband in two or three windows), per-channel power from the FFT we already
compute for the waterfall (noise-floor tracking + threshold = squelch), NFM discriminator or AM
envelope detector, 300–3000 Hz voice band-pass, de-emphasis, AGC, CTCSS via a 38-bin Goertzel bank
over ~200 ms windows (no ready CTCSS decoder in JS; goertzeljs exists). Voice-vs-carrier
discrimination: spectral-flatness test on the audio (Cascade-SDR's "voice squelch").

Scanner logic needs no libraries: channel grid per service (PMR446 446.00625 + n×12.5 kHz;
marine ITU App. 18; airband 25/8.33 kHz; IARU R1), scan list with lockout, priority channel,
hold after squelch close (1–3 s), auto-lock on strongest active, recording per transmission.
Because the HackRF is wideband, squelch runs on **all** channels in the captured 8 MHz at once and
only the active one is demodulated: no sequential retuning, strictly better than a hardware scanner.

Prior art: Cascade-SDR (GPL-3, Python server) has the most complete scanner spec (presets Marine /
PMR446 / Airband, dB-over-noise squelch, priority pre-emption, voice squelch, CTCSS/DCS readout);
BrowSDR / rad.io have per-VFO squelch and activity highlighting only.

Recommended: a "channel bank" worker stage: one FFT power vector per block → N squelch gates →
≤ 2 demodulators (NFM/AM) → mixer → AudioWorklet; CTCSS Goertzel on the audio; presets as JSON.
3–5 days.

## 5. TETRA / DMR / legal

**TETRA:** C2000 (NL) and ASTRID (BE) use TEA2 air-interface encryption (Meijer et al., USENIX
Security 2023, https://usenix.org/system/files/usenixsecurity23-meijer.pdf, names both networks).
TEA1 was broken; no practical break of TEA2. Passive listening is not possible. Do not build.

**DMR/dPMR:** BrowSDR PR #13 (merged 2026-10-06) adds mbelib-as-WASM with their own 4FSK sync;
DMR tested live (https://github.com/jLynx/BrowSDR/pull/13), AGPL. mbelib "may be covered by DVSI
patents" (RadioReference DSD wiki); Debian excludes it. Relevant unencrypted DMR in NL/BE is amateur
(Brandmeister on 70 cm). Stretch goal.

**Legal note (not legal advice):**
* *Netherlands:* art. 139c Sr, lid 2 exempts data received with a radio receiver unless a special
  effort was made or a non-permitted receiver was used. Plain reception is generally not an
  offence; decrypting is. Publishing received content is a separate question **[not researched]**.
* *Belgium:* BIPT "Standpunt inzake scanners"
  (https://belgiancycling.be/app/uploads/2022/05/Rchtlijnen_Scanners_BIPT_nl_fr.pdf): listening to
  CB, broadcast, amateur and PMR446 is fine; other bands without consent of the parties is
  prohibited, explicitly including emergency, police, aviation and maritime; a device is deemed
  illegal if non-permitted frequencies are pre-programmed in it *or in the PC controlling it*, or
  if it can decode encrypted/digital traffic (GSM, DECT, TETRA). Basis: Wet Elektronische
  Communicatie 2005 art. 124/145; Cass. 12 Dec 2023 narrowed art. 124 to knowledge of the
  *existence* of a communication, content under Sw. 259bis/314bis.
  **Implication:** FM/DAB/amateur/PMR446 presets are safe to ship; airband and marine presets
  should be opt-in with a jurisdiction notice; never TETRA.

## 6. Web Audio, resampling, FFT

* Resample as the last decimation stage so the audio rate lands on 48 k or 24 k (240 k → 48 k is
  5:1). Arbitrary ratios: `@alexanderolsen/libsamplerate-js` (WASM, streaming API).
* FFT: `webfft` (MIT, IQEngine) profiles several backends and picks the fastest; `pffft.wasm`
  SIMD is much faster than non-SIMD; fft.js is the pure-JS baseline. Demod barely needs FFTs; they
  matter for the channel-bank squelch and waterfall.
* Permissively licensed demod code: `@jtarrio/signals` (Apache-2.0), no-sdr `client/src/engine`
  (MIT). OpenWebRX `sdr.js` and BrowSDR are AGPL: avoid copying.

## Ranked build order

1. **WBFM stereo + RDS**: validates IQ → decimate → demod → AudioWorklet; port no-sdr (MIT). *(built 2026-10-28, own JS DSP rather than a port; RDS verified on synthetic signal, too weak on air here)*
2. **NFM/AM channel-bank scanner** (PMR446, amateur; airband/marine opt-in): same chain plus
   squelch/CTCSS; wideband capture makes it a true parallel scanner. *(built 2026-10-28, CTCSS 2026-10-29)*
3. **Live P2000/FLEX feed**: C glue to stream into the existing multimon-ng WASM. *(built 2026-10-29: option 2, `web/tools/src/mm_stream.c`; P2000 pages decode live from Willemstad NL)*
4. **DMR voice (amateur)**: only if the mbelib patent risk is accepted.
5. **DAB+**: Emscripten port of DAB-Radio (MIT) + FAAD2-WASM (GPL); weeks.
6. **TETRA**: never.

Status 2026-10-29: items 1–3 built; SSB/CW, APRS/DTMF/selcall/Morse, live rtl_433, RS41 and a satellite
pass predictor were added on top (see `docs/research-features-2026-10-29.md`). 4–6 unchanged.

## Not verified / gaps
* Whether Chrome's bundled FFmpeg decodes 960-frame HE-AAC today.
* Current encryption status of P2000 traffic after the 2018 renewal.
* CPU cost of a DAB OFDM + Viterbi pipeline in the browser.
* Whether `@jtarrio/signals` includes WBFM stereo and uses WASM internally.
* Dutch rules on *publishing* received traffic.
