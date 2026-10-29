# rfscan status / handoff

## Current state
* Working end to end: survey → plan → auto-gain dwell → async analysis → SQLite/events/report.
* Verified on real air (the location is actually **Belgium, Antwerp area**: MCC 206 GSM cells,
  aircraft near 51.2N 4.3E; P2000 from NL is also received):
  GSM (45 cells, SCH parity + SI3/4), LTE MIB CRC (17 cells, B1/3/7/8/20/28/69),
  NR n28/n78 PSS+SSS (sync), ADS-B CRC, P2000 FLEX BCH, BLE adv CRC, AIS CRC, DAB 12A/12C.
* LoRa and rtl_433 pass synthetic tests; there was no real traffic in test captures yet.

## Hardware gotchas (important)
* The device is a **HackRF Pro** (board 5). It needs firmware ≥ 2026.01.3 (flashed) and
  libhackrf 0.10 from `third_party/prefix` (rfscan loads it automatically). The system
  libhackrf 2024.02 / old firmware silently ignored the sample rate.
* Retuning or changing the rate while streaming corrupts samples. `hackrf.py` stops, reconfigures and restarts.
* USB drop-outs on low battery: the scheduler now reopens the device (retry for 120 s).

## Data
All IQ from 2026-10-19 is in `iq_archive/2026-10-19_antwerp/` (see `FINDINGS.md` and `INDEX.csv` there). `testdata/` is a symlink to its `reference_captures/`.
Live dashboard: `./rfscan.sh dashboard [run]`. Scan focus mode: `./rfscan.sh scan --focus odd`.

## Known issues / next steps
0. Capture timeout (fixed 2026-10-28): the 10-19 `ism433` timeout was a USB drop-out (HackRF vanished right after, low battery).
   `hackrf.capture` now fails within ~2 s when the stream stalls (clear message), and the scheduler reopens the device
   and retries that dwell once. Note: `rfscan capture` takes MHz/Msps (`capture 434.4 4 20 out`), not Hz.
0. Possible "track" mode: log 2290.45 / 2304–2314 MHz power over time for direction finding.
1. ADS-B decoder is ~2.4× slower than real time at 4 Msps. It's fine in the pool, but could be optimised.
2. b1_2140: 2139–2146 MHz looks like 30 kHz-SCS OFDM (NR?), but the NR decoder finds no SSB. Investigate.
3. 868 MHz: FSK bursts at 868.45 MHz (likely **Z-Wave**) and short bursts at 866.7/869.7 MHz are
   saved but not decoded. A Z-Wave decoder (FSK 9.6/40/100 kbps, checksum/CRC) would be a good addition.
4. NR MIB (polar decode) is not implemented, so NR stays at sync level.
5. Band plan is NL/CEPT; Belgian operators were added to the GSM map only. Consider a BE profile.
6. The `testdata/` regression set (2.1 GB) was recorded in the Antwerp area. Delete it if space is needed.

## Resume
```
./rfscan.sh scan --duration 3600          # new live run -> runs/<time>/report.html
./rfscan.sh report runs/<run>             # rebuild report
./rfscan.sh analyze runs/<run>/iq         # re-decode saved IQ offline
```

## Browser version (web/, 2026-10-28)
`web/index.html` + `web/bands.js`: browser-only rfscan via WebUSB (Chrome/Edge; needs https or localhost).
Serve: `python3 -m http.server 8090 --bind 127.0.0.1 --directory web`, open http://127.0.0.1:8090, click Connect once.
* Verified on the HackRF Pro: live RX at full 20 Msps (measured 19.7–20.05), AGC (rfscan/agc.py targets),
  SigMF recording, firmware sweep mode 1–6000 MHz at ~1.85 sweeps/s with region detection + band labels + "odd ones" filter.
* Offline tab works in any browser: 120 MB SigMF analysed in 0.7 s, same 2290/2304 MHz findings as Python.
* Only one tab/process can hold the HackRF: close the web tab (or it releases on Stop + reload) before running rfscan natively.
* Decoders: `web/decoder-worker.js` runs the real rfscan pipeline + decoders in Pyodide 0.27.2 (numpy, scipy, pyModeS).
  `web/build.sh` copies rfscan/*.py into web/py (rerun after changing rfscan/). In browser: lte, nr, gsm, adsb, ais, ble, lora;
  results identical to native on reference captures (LTE PCI 255 MIB CRC, GSM 11 cells, NR PCI 399, 9 aircraft with fields).
  Over the air: GSM 3 cells + LTE B8 PCI 468 MIB CRC from a live 1 s capture. Roughly 1.2-3x slower than native.
  pager / rtl433 in the browser (2026-10-28): decoder-worker.js routes their subprocess calls to WebAssembly
  builds of multimon-ng 1.3.1 / rtl_433 25.02 in web/tools, made by `web/build_tools.sh` (emsdk 4.0.10 in
  third_party/emsdk, ~1 GB; needs -sSTACK_SIZE=8MB or multimon-ng crashes). Verified: P2000 reference capture
  gives the same 4 real pages as native (native also prints one noisy duplicate); synthetic EV1527 remote
  decodes identically. Without web/tools those two decoders fall back to native-only automatically.
  Native-only: dab (welle-cli), GSM SI3/4 MCC/LAC (grgsm) and LoRa payload (gr-lora_sdr);
  GSM in the browser stops at BSIC (SCH parity). Capture lengths are capped (BROWSER_MAX_S) to fit WebAssembly memory.
  Browser decode robustness: a capture whose USB stream stalls fails after 3 s (STALL_MS) instead of hanging
  with the busy flag set; a second click while busy is refused; "Reset stuck decode" on the Decodes tab
  releases the flag and idles the radio.
* Audio tab (2026-10-28): `web/audio-worker.js` (NCO mix -> polyphase FIR decimation to ~250 kHz -> WFM stereo+RDS /
  NFM / AM -> 48 kHz) + `web/audio-worklet.js` (ring-buffer player). Live capture is 10 Msps (HackRF aliases below 8 Msps).
  Verified: synthetic stereo FM + RDS (`tests/web/make_fm_rds.py`, `tests/web/test_audio_worker.js`): PI/PS/RT/PTY decode,
  0 bad blocks, L/R separation ~10 dB; real fm_98 capture: stereo pilot lock + RDS block sync (file too short for groups);
  live: 10 Msps sustained, DSP 27 % of real time. No FM received here today (antenna), so live FM audio is untested on air.
  Live FM on air (whip, 2026-10-28 pm): stereo lock and audio OK on 102.7 MHz; RDS fails because the station is only
  ~11 dB CNR here (57 kHz subcarrier ~2 dB above the demodulated noise); captures in testdata/fm_live_*.
* Channel-bank scanner (same day): `ChannelBank` in audio-worker.js: 4096-pt FFT per block, channel power = centre bins,
  floor = 30th percentile over neighbouring channels (time-smoothed), 2-block debounce, splatter suppression, hold,
  priority, lockouts, events. Page hops 10 MHz windows when idle. Tests: `tests/web/make_pmr_synth.py` +
  `tests/web/test_scanner.js` (locks ch3/8/14/3 as scripted); live PMR446 quiet (all channels within ±4 dB of floor);
  FM-band scanner auto-locks on 102.7 MHz.
  Audio quality pass (same day, after "crackle" report): stereo blend from a pilot-to-noise gauge (80 kHz MPX band;
  mono below ~2 dB, full stereo above ~10 dB on that gauge), 180 kHz IF, AGC frozen once settled (gain steps click),
  player-fill -> resampler rate trim (holds 0.3 s), soft limiter, preallocated resampler. Offline on the 102.7 MHz
  recording: impulses 89/s -> 0.3/s; live: buffer 0.29 s steady, 0 underruns over 35 s. Threshold hiss on weak stations remains.
* Live pager feed (2026-10-29): `web/tools/src/mm_stream.c` exports mm_stream_open/feed/close from the multimon-ng WASM
  (`web/build_tools.sh multimon-ng` rebuilds just that tool); `PagerTap` in audio-worker.js takes the raw discriminator
  -> 6 kHz LPF -> 22050 Hz s16 (±4.8 kHz = ±12000, as native pager.py), `PagerDecoder` feeds it per block and parses the
  FLEX|...|ALN|text / POCSAG lines. Verified: `tests/web/test_pager_stream.js testdata/p2000_169.sigmf-data 169.95 2 169.65`
  gives the same 4 pages as native (13x real time at 2 Msps); live on air from Antwerp with the whip: 4 real P2000 pages in
  the first 40 s (signal -23 dB, DSP 25 %). Preset "P2000 pagers" is opt-in (BIPT) and mutes the audio by default.
* 2026-10-29 evening (web): SSB/CW demod (`SsbDemod`), CTCSS Goertzel bank, multimon-ng streaming now also runs
  AFSK1200 (APRS, aprs_mode=1 -> "APRS: SRC>DST,PATH:payload"), DTMF, ZVEI/CCIR/EEA/EIA, MORSE_CW; data-only extra
  receivers (`vfo` / `vfo-del` worker commands, status.vfos); page: Map tab (Leaflet, cdnjs; ADS-B/AIS from decodes,
  APRS from the data decoders), bookmarks (localStorage `rfscan.bm`), WAV recording + per-transmission recordings,
  data VFO row, HF presets, header device panel (bias tee = vendor request 23, CLKOUT = 32; temperature request 62
  returns nothing on API 1.10 so it stays blank). Tests: `tests/web/make_aprs_synth.py` + `test_audio_modes.js`
  (APRS position, CTCSS 100 Hz, DTMF 1234#, USB/LSB tone) all pass. Live: P2000 main VFO + POCSAG data VFO on
  172.4547 MHz decode together in one 10 MHz capture, DSP 30 %.
  Not done: 16-bit sample mode of the HackRF Pro (no libhackrf 0.10 API for it; only the FPGA "narrowband filter"
  request 53, effect on the stream unknown, left out), RS41 radiosonde decoder (needs the rs1729/RS source + a sample
  recording from the internet; not fetched yet).
  Python changes for Pyodide: `_ofdm_util.pmap` (no threads on emscripten), `recording.iq` reads via fromfile (no mmap).
* Scan waterfall is 2048 columns over the full range, so it is coarse when zoomed.
* Native `rfscan capture` hit "capture timeout" at 1089 MHz/20 Msps today while hackrf_transfer worked (see item 0).

## Long-term monitoring (rfscan/monitor.py, 2026-10-29 evening)
Per run: time-vs-frequency heatmap of all surveys, per-band occupancy over time and **new-signal alerts**
against a baseline (earlier surveys of the run, plus optionally a baseline run). numpy only (no matplotlib/PIL
in the venv): the PNG is written by a small built-in encoder, the report embeds it in an SVG with axes.
* `./rfscan.sh monitor <run> [--baseline <run>] [--f-tol-khz 150] [--db-tol 6] [--min-snr 10] [--min-persist N] [--log-events]`
  prints the new-signal list (persistent ones first, one-offs last) and writes `<run>/monitor/heatmap.png`,
  `heatmap.npz` and `summary.json` (surveys, new signals with persistence, band occupancy series).
  A region is "new" when no baseline region within f_tol was within db_tol of its SNR **and** the baseline
  max spectrum (mean power above floor) within f_tol is more than db_tol below it; `persisted` = later
  surveys of the run that still show a region there. Spur-flagged regions are ignored on both sides.
* Live: `rfscan scan [--baseline <run>]` runs the same `monitor.Tracker` after each survey (milliseconds, in
  memory), logs `NEW SIGNAL` lines, writes `new_signal` events to events.jsonl and refreshes `monitor/`.
  The scan loop is otherwise unchanged. `report.html` (and so the dashboard) has a "Monitoring" block:
  heatmap with first-appearance markers, new-signal table (one-offs collapsed), band occupancy sparklines;
  it reuses `monitor/` when it covers all surveys, else recomputes without a baseline.
* With a baseline run the heatmap gets the baseline surveys as top rows (red separator), so it also works across runs.
* Test: `.venv/bin/python tests/monitor/test_monitor.py` (also pytest-compatible): runs on today's run with
  the 10-19 baseline, injects a +30 dB region into a copy of the last survey, checks it is reported new while a
  region copied from the previous survey is not, and checks heatmap.png/npz + summary.json contents.
* Validated on `runs/20261029_104759` vs `iq_archive/2026-10-19_antwerp/scan1_full_1400`: 189 new regions, of
  which 144 are one-offs (almost all in the noisy first survey 10:48: 3.5–3.7, 4.1–4.2, 4.9, 5.75–5.9 GHz, 30 dB,
  occupancy 0.05–0.2; likely sweep glitches). Persistent: the **2289.79 MHz** (2.06 MHz, 21.5 dB, baseline 7 dB) and
  **2284.55 MHz** (1.3 MHz, 14.8 dB) bursts in all 6 surveys (2297.25 MHz was already there on 10-19); the **172.464 MHz POCSAG** channel
  (14.3 dB, occupancy 0.57, first in survey 2 at 11:04, 8 dB in survey 1, absent on 10-19); plus things that
  differ from the 10-19 site: GSM 900 DL at 940 MHz (+32 dB), LTE B20 813 MHz, B32 SDL 1471/1478 MHz, 1120 MHz DME,
  TETRA 390.5/384.5 MHz. The first survey of a run is the noisiest; `--min-persist 1` hides the one-offs.
