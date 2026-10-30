# rfscan status / handoff

## Current state
* Working end to end: survey → plan → auto-gain dwell → async analysis → SQLite/events/report.
* Verified on real air. **Location (confirmed by the user 2026-10-30): Willemstad, Noord-Brabant, NL,
  ≈51.69N 4.44E.** Earlier notes, the `iq_archive/2026-10-19_antwerp` directory name and the first reports
  wrongly assumed Antwerp/Belgium; the Dutch GSM/LTE cells and P2000 pages are simply the local networks.
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
0. "Track" mode is now `rfscan monitor` (heatmap, occupancy, new-signal alerts, 2026-10-29); the 2.27–2.29 GHz
   bursts are still unidentified: next is a 20 MHz capture at 2285 MHz and a look at the burst timing.
0. Browser ideas not built (see docs/research-features-2026-10-29.md): ORBCOMM decoder, Meteor-M2 LRPT (needs a
   137 MHz antenna), DAB+, DMR (patent risk), ACARS/VDL2 (opt-in only, BIPT), HackRF Pro 16-bit mode (no API).
1. ADS-B decoder is ~2.4× slower than real time at 4 Msps. It's fine in the pool, but could be optimised.
2. b1_2140: 2139–2146 MHz looks like 30 kHz-SCS OFDM (NR?), but the NR decoder finds no SSB. Investigate.
3. 868 MHz: FSK bursts at 868.45 MHz (likely **Z-Wave**) and short bursts at 866.7/869.7 MHz are
   saved but not decoded. A Z-Wave decoder (FSK 9.6/40/100 kbps, checksum/CRC) would be a good addition.
4. NR MIB (polar decode) is not implemented, so NR stays at sync level.
5. Band plan is NL/CEPT; Belgian operators were added to the GSM map only. Consider a BE profile.
6. The `testdata/` regression set (2.1 GB) was recorded at Willemstad (NL). Delete it if space is needed.

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
  gives the same 4 pages as native (13x real time at 2 Msps); live on air from Willemstad with the whip: 4 real P2000 pages in
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
* RS41 (later that evening, after the user's OK): `web/build_tools.sh rs41` clones rs1729/RS (GPL-3.0, commit 89b1ea7)
  and builds demod/mod/rs41mod as web/tools/rs41.{js,wasm}; `web/rs41-live.js` runs it in IQ mode (`--iq2 --ecc --ptu
  --crc --json`) on 4 s windows (1 s overlap) of 50 kHz complex baseband written as 8-bit stereo WAV, ~50 ms per window;
  frames de-duplicated by id+frame. PTU (temperature/humidity) stays empty because the calibration subframes do not
  survive the per-window module. Test: `tests/web/test_rs41.js RS41.wav` with the sigidwiki sample
  (https://sigidwiki.com/images/4/43/RS41.zip, 75 kHz 8-bit IQ, sonde U4345245 near Bratislava): 32 frames in 32 s,
  all with position. Browser: 'RS41' decoder option / "Radiosondes 400-406" preset / data VFO; verified end to end with
  the sample upsampled to a 2 Msps ci8 file (web/rs41_test.cs8, git-ignored): 13 frames from 14 s, marker on the Map tab.
  Not yet heard live (De Bilt / Ukkel launches ~01:00 and ~13:00 local).
* Satellites tab + "Where am I?" (same night): see README. Where-am-I sources: minimax centre of received emitter
  positions with a reception-range accuracy (ADS-B 300 km, AIS/APRS 60 km, sondes 300 km), P2000 heard -> NL ±260 km,
  GSM MCC -> country, beaconDB cell geolocation (CORS preflight to api.beacondb.net answers 404, so the browser call
  fails and the panel prints the equivalent curl; from a shell it returns e.g. 51.68,4.44 ±19 km for the KPN LAC 3350
  cells, i.e. the Dutch side of the border), fused by inverse variance. Verified with 3 ADS-B aircraft from the
  reference capture (51.64, 4.42 ±300 km), live P2000 pages and the RS41 test file (which pulls the fused point
  east, as a Bratislava sonde should).
  Python changes for Pyodide: `_ofdm_util.pmap` (no threads on emscripten), `recording.iq` reads via fromfile (no mmap).
* Live rtl_433 (2026-10-29): `web/rtl433-live.js` (plain script, importScripts/require; not wired into index.html yet).
  `new Rtl433Live(create_rtl433, wasmBytes, {rate, centerHz, onMessage, chunkS: 2, mergeWindow: 0.25, dedupeS: 1, format: 'cs16',
  extraArgs: [], extraProtocols: [104, 105, 238]})`; `feed(I, Q)` (Float32 baseband at `rate`, |x| <= 1), `flush()`, `stats`
  {chunks, decodes, dupes, msPerChunk}. Every 2 s chunk (+ the last 0.25 s of the previous one) is written as cs16 into a fresh
  rtl_433 module and decoded with `-r -s -f -F json -M level -M protocol -M time:rel -R <all defaults + wM-Bus C/T, S, T>`;
  onMessage gets the rtl_433 JSON fields plus {t (ms epoch, estimated), ts (stream s), freqHz, chunk}. A fresh module per chunk
  is required (EXIT_RUNTIME=1: after exit() the FS streams are closed, a second callMain prints nothing) and costs only ~2 ms
  (V8 caches the compiled module). `IqChannelizer(inRate, offsetHz, outRate)` in the same file does NCO + FIR decimation from
  int8 IQ. Cost per 2 s chunk (Node 20, this laptop): 1 Msps 72 ms (46 ms rtl_433 + 10 ms pack + 2 ms module + 2 ms FS), 250 k
  37 ms; cu8 is slower to pack (45 ms) and cf32 slower to decode (62 ms). Channelizer: 10 Msps -> 250 k/1 M 27 % of a core,
  20 Msps -> 1 M 47 %. Rates: 250 k for 433 MHz OOK/FSK (433.92), 1 M for 868 MHz wM-Bus (868.95 C/T, 868.3 S/T).
  Verified: `tests/web/test_rtl433_live.js <ci8> <centreMHz> <rateMsps> [--tune MHz:kHz] [--expect model:id]`. The 10-19
  reference captures testdata/ism433 + ism868 hold no rtl_433-decodable packet (native rtl_433 and rfscan's rtl433.py find none
  either; the ELS 36325352 gas meter was decoded live on 10-29 from a watch capture whose wide IQ was not kept), so
  `tests/web/make_ism_synth.py` rebuilds that meter's wM-Bus mode C telegram bit-exact from the logged payload plus a Nexus-TH
  OOK sensor (tests/web/ism868_synth, ism433_synth, 2 Msps, 8 s). Results: wM-Bus 3/3 (id 36325352, CRC, incl. one frame
  across the 4 s chunk border that is lost with mergeWindow 0; the 12 dB one at snr 17 dB), Nexus-TH 3/3, identical to native
  rtl_433 on the same channelized data; 0 false decodes on the 60 s real captures (3-7x real time including channelizing).
  Done the same evening: 'RTL433' decoder option in the Audio tab (main receiver or data VFO, `IsmVfo` in audio-worker.js),
  presets ISM 433 / SRD 868; live at 868.95 MHz 61 ms per 2 s chunk, no telegram during the 5 min tried.
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

## Passive localisation (2026-10-30, three subagents on Opus at the user's request)
Receiver is in **Willemstad NL (≈51.69N 4.44E)**, which the user confirmed this morning; the night test of
2026-10-29 was inside a steel building. Captures for this work: `runs/locate_20261030/` (DAB 207/224 MHz 10 s and
12C/11C/9C 60 s at 2.048 Msps; n78 3465…3700 MHz 2 s; LTE B8/B20/B3/B7/B32 1 s; GSM 937 MHz 6 s).
* **Offline cell database** (`rfscan/cellpos.py`, `web/cells/{204,206,262}.csv.gz`, 12 MB, GSM only): beaconDB has
  no export, OpenCelliD needs a token, so the data is the final Mozilla Location Service cell export (archive.org
  item `MLS_Full_Cell_Export_Final`, CC0, 2024-03). Lookup by MCC/MNC/LAC/CI, power- and 1/range-weighted estimate,
  LAC-centroid fallback only when no CI matches. Result: run 10-29 → 51.688, 4.436 (0.37 km off); gsm_937 → 0.56 km
  off; stated ± is ~15 km because MLS ranges are 17–32 km. KPN CIs 56061/56062 are newer than MLS. Browser: Where-am-I
  checkbox "cell DB (offline)" (fetch + DecompressionStream) and an "extra estimates JSON" paste box. Tests:
  `tests/cells/test_cellpos.py`, `tests/web/test_celldb.js`. LTE/NR cells cannot be matched (databases key on
  ECI/TAC; we only decode PCI + MIB) → next step is SIB1 decoding. Notes: `docs/notes-cells-offline.md`.
* **DAB TII TDOA** (`rfscan/tdoa_dab.py`, `rfscan/data/dab_tx.json` with 398 NL/BE transmitters incl. TII codes from
  radio-tv-nederland.nl / wohnort / fmtvdx and positions from the Antenneregister WFS): own numpy TII decoder
  (welle-cli's TII is integer-sample only and loses sync at the -5 dB channel SNR here), coherent null-symbol
  averaging over the TII-bearing alternate frames, lobe disambiguation against the PRS impulse response, weighted
  Gauss-Newton hyperbolic solver. Finds Roosendaal (12C 4-10, 11C 21-20, 9C 19-20) and Rotterdam (9C 19-05, 12C 4-05
  tentative) at 20–27 dB; Dordrecht/Breda stay 5 dB under the threshold, so **no fix yet (needs ≥ 3 sites)**.
  A 60 s capture (`--max-s 60`) lifts Roosendaal from 27 to 34 dB as expected, but no further site appears: indoors the SFN is effectively one transmitter. Also found: the SFN is not delay-free; Roosendaal transmits ≈29 µs
  late relative to Rotterdam (both 9C and 12C agree), so per-site `offset_us` calibration is needed before a fix
  can be trusted. Captures at 20 Msps show USB sample drops (frame spacing jumps), handled per frame. Test:
  `tests/tdoa/test_dab_tii.py` (synthetic 5-transmitter network: 5 m error). Notes: `docs/notes-dab-tii.md`.
* **NR SSB TDOA** (`rfscan/tdoa.py` generic solver, `rfscan/tdoa_nr.py`, opt-in timing additions in
  `rfscan/decoders/nr.py`: sub-sample PSS timing, case-C frame-start, SSB index, periodicity, stronger-cell
  cancellation): synthetic 3-cell test recovers offsets within 5 ns and a position within 10 m. **No real fix:**
  none of the seven n78 windows (3465–3710 MHz) contains an SSB indoors; only uplink bursts at 3580 and a 4 MHz
  signal at 3457 MHz. LTE: never two cells in one 10-30 capture; in the 10-19 `lte800_806` capture PCIs 334/497
  differ by a stable 1.23 µs (std 0.13 µs), which looks like phase-synchronised FDD, one pair only. Site data:
  67 n78 masts within 15 km from the Antenneregister WFS, operator by 2024 auction block (VodafoneZiggo 3450–3550,
  Odido 3550–3650, KPN 3650–3750), nearest KPN+Odido mast 260 m away; no public PCI→site mapping, so
  `rfscan/data/nr_sites_willemstad.json` holds candidates and `--assign` searches assignments (low confidence).
  HackRF sample-clock drift varies −0.4…+5.9 ppm between captures, so cells are only compared within one capture
  against an interpolated reference. Test: `tests/tdoa/test_tdoa_nr.py`. Notes: `docs/notes-nr-tdoa.md`.
* **Next steps:** outdoors (or at a window) repeat the n78 and DAB captures; DAB with the front-end amp on; calibrate
  SFN offsets at the known position; SIB1 decoding for LTE cell identities; then wire the three native tools into
  the browser (they already emit the Where-am-I estimate JSON).
* **Release report (2026-10-30):** `tools/release_report.py` + `docs/localisation_status.json` → PDF following the
  Huisstijlhandboek Profile Equipment v1.0 (repo PiMo131/huisstijlen, private): Aptos/Arial (Liberation Sans on CI),
  Profile donker #474220 for h1 and table heads, Profile goud #C2A202 for h2 and accents, near-black text, logo in
  colour top right on every page (< 4.5 cm, `docs/report/logo.svg` = the official 'Profile Equipment - kleur.svg'),
  footer with document name and 'Pagina X van Y' in 8 pt grey, no dark blocks or effects. `.github/workflows/release.yml` builds it on every `v*` tag and creates the GitHub release with the PDF.
  First release: v0.0.3.
