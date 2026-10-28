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
  Python changes for Pyodide: `_ofdm_util.pmap` (no threads on emscripten), `recording.iq` reads via fromfile (no mmap).
* Scan waterfall is 2048 columns over the full range, so it is coarse when zoomed.
* Native `rfscan capture` hit "capture timeout" at 1089 MHz/20 Msps today while hackrf_transfer worked (see item 0).
