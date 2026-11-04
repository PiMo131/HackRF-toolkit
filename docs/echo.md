# Echo tab — adaptive DMR-like scan, fragment library, offline replay and Lab TX

The Echo tab adds, to the existing browser app: a checkbox multiselect of receive-frequency profiles, an
adaptive scan engine (energy survey → candidate ranking → focused dwell → compact IQ capture → background
revisit), a persistent fragment library, offline replay, and a deliberately-boxed-in lab transmitter.

**What it is not.** No decoder for encrypted DMR audio, no crypto bypass, no MOTOTRBO/network emulation, no
real repeater. No free/automatic over-the-air replay, no `RX frequency → TX frequency` coupling, no automatic
transmit on a discovered channel. A delayed IQ replay is not claimed to be a valid DMR network transmission.

## Modules

| file | role | runs in |
|---|---|---|
| `web/echo-profiles.js` | profiles, interval union, firmware-sweep ranges, RX-window planning, versioned `localStorage` | page + worker + node |
| `web/echo-dsp.js` | channelizer, DMR-like classifier, `ci8` store, TX interpolator + spectrum check, sweep parser, synthetic signal generators | page + worker + node |
| `web/echo-capture.js` | per-channel trigger: pre-roll, debounce, hang-time, hard cap + splitting, `late_lock` | worker + node |
| `web/echo-scheduler.js` | `EchoScheduler` (pure planning) + `EchoEngine` (drives a radio) | worker + node |
| `web/echo-sim.js` | `SimRadio` — scripted emitters with a simulated clock, same interface as the real radio | node + demo |
| `web/echo-storage.js` | `EchoLibrary` (IndexedDB / in-memory), SigMF import/export, quota, integrity | page + node |
| `web/echo-replay.js` | `ReplayQueue` — offline virtual replay, no RF | page + node |
| `web/echo-lab-tx.js` | `LabTxController` + `TxAllowlist` — guarded real transmit | page + node |
| `web/echo-worker.js` | hosts `EchoEngine` off the UI thread; drives the HackRF through a message protocol | Web Worker |
| `web/echo.js` | UI controller; owns the HackRF for the worker and for Lab TX | page |

The heavy scan DSP runs in the worker. The HackRF stays a single WebUSB owner on the main thread: the worker
asks it to sweep or receive, the main thread streams raw USB bytes back tagged by mode, and the worker parses
them. Live/Scan/Audio/Echo never claim the device at once.

## State machine

```
IDLE → DISCOVERY → RANK_CANDIDATES → FOCUSED_DWELL → [CAPTURING → FINALIZING → SAVED] → MAINTENANCE → DISCOVERY
```

plus `PAUSED` (half-duplex: the radio is handed to Lab TX), `STOPPING`, `ERROR`, and the Lab TX subsystem
states `OFF / ARM_REQUESTED / ARMED / TRANSMITTING / COOLDOWN / FAULT`.

* **DISCOVERY** — a `hackrf_sweep`-style energy survey over the merged selected ranges (several passes so
  one-pass blips and spurs don't become candidates). Energy only makes an `energy_candidate`.
* **RANK_CANDIDATES** — score = f(SNR, repeat hits, recency, DMR history) − spur/inconclusive/always-on
  penalties. Candidates within one raster step are merged (centroid jitter between surveys).
* **FOCUSED_DWELL** — stop sweep, normal RX at a stable rate (default 8 Msps), channelise the candidate(s) to
  50 ksps, classify. One window can hold a few nearby channels (`windowFor`).
* **CAPTURING / FINALIZING / SAVED** — on a detection at or above the configured level, record compact IQ.
* **MAINTENANCE** — hot/warm/cold revisit scheduling; a full survey is forced after `maxNoSurveyS`.

## Classification (physical layer only)

`classifyDmrLike` labels a 50 ksps complex capture as `none / activity / unknown_digital / possible_dmr /
confirmed_dmr`. It measures a channel-power duty cycle (SNR), a symbol-rate spectral line (digital vs
analogue), the 4-level fit and the outer-symbol deviation (~1944 Hz), and searches for the ETSI TS 102 361-1
sync words (BS/MS voice/data, normal and inverted). `confirmed_dmr` requires sync detection **and** a
DMR-like deviation; it is never claimed from energy alone, and there is no voice decode or decryption.

## Compact IQ storage

RX runs at a stable high hardware rate; per candidate the signal is complex-downmixed to the channel centre,
anti-alias filtered and decimated to **50 ksps complex**, stored interleaved signed 8-bit (`ci8`) with a
recorded gain and clip statistic. 50 000 complex samples/s × 2 bytes ≈ **100 kB/s → 3 MB/30 s, 6 MB/60 s**.
100/50/25 ksps are available as test variants; 50 ksps is a working hypothesis, not a proven final value.
Export/import is SigMF (`.sigmf-meta` + `.sigmf-data`) with `rfscan:echo_*` metadata; round trips are
bit-exact. `IQ centre` (channel) and `RF TX centre` are kept as distinct concepts — there is no automatic
transmit on a received frequency.

## Offline replay / Auto Echo

`Play` replays a fragment through the same analysis chain as a baseband preview — **no RF energy**. Repeats
1–10, pause 0–5000 ms, bounded queue (≤20), Stop and Emergency Stop. `Auto Echo` (off by default) replays
new clips the same way. In Lab TX mode the same queue may instead send a bounded run of **your own**
synthetic/lab fragments to the pre-authorised lab frequency — never arbitrary intercepted traffic.

## Lab TX (real transmit, lab-only)

A real software TX pipeline: resample the owned/synthetic 50 ksps baseband to a HackRF-supported TX rate with
an anti-imaging filter, shift it off DC with an LO offset, limit the peak, quantise to int8, and stream it
paced on the TX endpoint (read from the live USB descriptor, not an assumed number). The sequence ends
**unconditionally** with `MODE.OFF`, including on exception, stop or disconnect.

Hard rules (enforced in `echo-lab-tx.js`, not just the UI):

* RF TX is **off after every page load, fault or reconnect**; nothing in Auto Echo can arm or transmit.
* A **separate, empty-by-default allowlist** supplies the TX centre frequency; it is independent of the RX
  presets and candidates and is never written from discovery.
* Only `synthetic` or `owned_lab_recording` sources transmit; `live` and imported-unknown never do.
* A user-confirmed lab setup (dummy load / attenuator / shielded, or wired second receiver) is required — and
  the code notes that a checkbox is not physical proof.
* Two-step **Enable + Arm** with visible expiry, fixed centre, bounded single-clip duration and max repeats.
* One transmission at a time, a hard timeout independent of the UI queue, forced RX re-acquire cool-down,
  Emergency Stop, and re-arm required after any fault.

See the README "Lab TX — hardware verification protocol" for what must be measured before it may be called
`hardware-verified`.

## Tests

`tests/web/echo_*.js` (run `tests/web/echo_run.sh`, or CI `.github/workflows/echo-tests.yml`):

* `echo_profiles` — union/merge, sweep ranges ≤10, RX windows, channel-list import, persistence.
* `echo_dsp` — classifier across SNR / offset / inverted / FM / noise / 2-FSK; two signals in one window;
  `ci8` round trip; TX interpolation spectrum + image suppression + loopback; sweep parser.
* `echo_capture` — pre-roll, hang-time, hard cap + split, `late_lock`, separate bursts, detect gating.
* `echo_scheduler` — the scenario from the spec: silent band → one hot candidate 3× then gone → later new
  cold-band activity; assert hot gets more dwell, cold is still revisited, no duplicate visits; plus no
  starvation under a continuously-active source, and honest coverage reporting.
* `echo_storage` — ~3 MB/30 s, SigMF round trip bit-exact, quota, integrity, and offline replay that calls
  **no** transmit path.
* `echo_lab_tx` — every safety gate: refusals (disabled / empty allowlist / unverified / not armed / wrong
  source / too long / expired), gain cap, USB drop → FAULT + RF OFF, hard timeout, emergency stop, device
  lost, and that nothing but `armFor()` reaches `ARMED`. Every path asserts the device ends RF-OFF.
* `echo_browser` — headless Chromium drives the real page with a mock HackRF (synthetic sweep + RX streams):
  Start → survey → focused dwell → a **confirmed-DMR** clip in the library → Auto Echo replays offline with
  **zero** WebUSB TX bytes; Lab TX arming gated in the real DOM.
