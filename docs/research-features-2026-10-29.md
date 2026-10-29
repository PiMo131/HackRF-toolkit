# Feature research, 2026-10-29: other HackRF projects, and satellites

Web research done on 2026-10-29 to decide what to add to the browser version. Compared against
BrowSDR, BrowserSDR, rad.io, webRx, SDRangel, OpenWebRX+ and PortaPack Mayhem. What we already had
that most of them lack: the autonomous 1–6000 MHz survey with classification, LTE/GSM/NR decoders to
CRC level, offline SigMF analysis, the channel-bank scanner.

## Gap list and what was done the same day

| Item | Source of the idea | Effort | Status |
|---|---|---|---|
| APRS/AX.25 (AFSK1200), DTMF, ZVEI/CCIR/EEA/EIA, Morse | multimon-ng already in our WASM; every browser SDR has APRS | hours | **done** (`mm_stream.c`, `aprs_mode=1`) |
| SSB (USB/LSB) and CW | BrowSDR, BrowserSDR; HackRF Pro tunes to 100 kHz | hours | **done** (`SsbDemod`) |
| Map for ADS-B / AIS / APRS | all of them | hours | **done** (Leaflet, Map tab) |
| Bookmarks, WAV recording, per-transmission recording | BrowserSDR (clips, rewind), Cascade-SDR | hours | **done** |
| CTCSS readout | Cascade-SDR | hours | **done** (Goertzel bank) |
| Live rtl_433 (433 sensors, 868 wM-Bus) | BrowSDR, Mayhem | days | **done** (`rtl433-live.js`, cs16 chunks into a fresh module) |
| RS41 radiosonde + map | BrowSDR, SDRangel, Mayhem | days | **done** (rs1729/RS rs41mod WASM, IQ mode) |
| Multiple VFOs | BrowSDR (unlimited VFOs) | days | **done** as data-only extra receivers |
| Long-term monitoring: heatmap, occupancy, new-signal alerts | hb9tf/spectre, Ossmann BH2017 | days | **done** natively (`rfscan monitor`) |
| HackRF Pro extras | GSG Pro Q&A, libhackrf 0.10 | hours | bias tee + CLKOUT **done**; 16-bit mode has no libhackrf API (only the FPGA "narrowband filter" request 53, effect unknown), temperature request 62 returns nothing on API 1.10 |
| DAB+ audio | BrowserSDR claims it | weeks | not done (see research-live-audio.md) |
| ACARS / VDL2 | OpenWebRX+, BrowSDR | days | not done; airband listening is not permitted in Belgium (BIPT), would be opt-in |
| DMR / NXDN / D-STAR via mbelib | OpenWebRX+ (separate image), BrowSDR PR #13 | days | not done, patent risk |
| Weather satellite images | SDRangel, Mayhem | weeks | not done, see below |
| Transmit (Mayhem) | | | deliberately never |

## Satellites with a HackRF Pro and a whip indoors

* **NOAA APT is over**: NOAA-18 decommissioned 2025-06-06, NOAA-19 2025-08-13, NOAA-15 2025-08-19
  (NOAA OSPO status page, NESDIS notice). Do not build an APT decoder.
* **Meteor-M2-3 (137.9 MHz) and Meteor-M2-4 (137.1 MHz) LRPT** are the remaining 137 MHz image
  satellites: QPSK 72 kbaud, Viterbi + Reed-Solomon, JPEG-like segments. SatDump-class decoder, weeks;
  needs a V-dipole or QFH outdoors, the whip indoors will not do. The original Meteor-M2 failed in
  December 2022.
* **ISS**: APRS digipeater 145.825 MHz (our AFSK1200 decoder works for it, preset added), FM
  cross-band repeater downlink 437.800 MHz, SSTV events on 145.800 MHz (no SSTV decoder yet).
* **Amateur FM satellites** (SO-50 436.795, AO-91 145.960) and **RS-44** linear transponder (USB
  435.610–435.670): NFM/USB listening with Doppler tracking. Built: Satellites tab (satellite.js,
  bundled TLEs refreshed daily because Celestrak sends no CORS headers).
* **ORBCOMM** 137.2–137.8 MHz: strong, simple protocol, decoders exist (e.g. orbcomm-receiver).
  Medium. Passes are predicted; first attempt at 10° elevation with the whip showed nothing above the noise.
* **Inmarsat (1.5 GHz STD-C, AERO ACARS) and Iridium (1.6 GHz)**: in the radio's range but need a
  patch antenna with LNA (the bias tee can power it) and heavy decoders (JAERO, scytale-c, gr-iridium).
* **DVB-S**: only with dish + LNB (IF 950–2150 MHz is inside the HackRF range); decoder (leandvb) large.
* **GPS, Starlink, GOES**: not realistic in the browser.

## Sources
* BrowserSDR: https://github.com/ShiftToRight/browsersdr (PolyForm Perimeter licence)
* BrowSDR: https://github.com/jLynx/BrowSDR (AGPL-3.0); rad.io: https://github.com/alexthemitchell/rad.io; webRx: https://github.com/LeeMcQ/webRx
* PortaPack Mayhem receivers: https://github-wiki-see.page/m/portapack-mayhem/mayhem-firmware/wiki/Receivers
* SDRangel: https://www.rtl-sdr.com/?p=57076 ; OpenWebRX+: https://cq.sk/en/openwebrx-web-sdr-with-digital-mode-decoders/
* HackRF Pro: https://greatscottgadgets.com/2025/06-27-hackrf-pro-q-a , https://hackrf.readthedocs.io/en/latest/hackrf_pro.html
* Long-term monitoring: hb9tf/spectre via https://www.rtl-sdr.com/tag/hackrf_sweep/ ; Ossmann & Spill, Black Hat 2017 "What's on the wireless"
* NOAA POES status: https://www.ospo.noaa.gov/Operations/POES/status.html , https://www.nesdis.noaa.gov/news/suspension-of-poes-data-users-after-june-16-2025-and-decommissioning
* Meteor: https://db.satnogs.org/satellite/59051 , https://www.rtl-sdr.com/the-meteor-m2-lrpt-weather-satellite-has-failed/
* RS41 decoder and sample: https://github.com/rs1729/RS (GPL-3.0), https://sigidwiki.com/wiki/Vaisala_RS41-SG_Weather_Balloon_(Radiosonde)
* radiosonde_auto_rx sample format notes: https://github.com/projecthorus/radiosonde_auto_rx (auto_rx/test/README.md)
