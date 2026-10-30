# DAB TII / SFN TDOA positioning: notes (2026-10-30)

Code: `rfscan/tdoa_dab.py`. Database: `rfscan/data/dab_tx.json`. Tests: `tests/tdoa/test_dab_tii.py`.

```
.venv/bin/python -m rfscan.tdoa_dab runs/locate_20261030/dab_224.sigmf-meta runs/locate_20261030/dab_207.sigmf-meta
.venv/bin/python -m rfscan.tdoa_dab runs/x/dab_224.sigmf-meta --blocks 12C,11C --json-only
.venv/bin/python tests/tdoa/test_dab_tii.py
```
The last stdout line is a JSON list `[{"src":"dabtii","lat":..,"lon":..,"acc":..,"n":..,"note":..}]` for the
browser Where-am-I paste box. It is empty (`[]`) when there is no credible fix.

## Why there is no welle-cli route
welle.io 2.x (`src/backend/tii-decoder.cpp`, `src/welle-cli/webradiointerface.cpp`) has a TII decoder, and
`/mux.json` reports `tii: [{comb, pattern, delay, delay_km, error}]`. That delay is a whole number of samples
relative to its FFT window. It comes from a brute-force phase-slope search with 5 measurements and a median.
Run on the 9C channel extracted from `dab_207`, `welle-cli -w` produced 396 FIC CRC errors, no ensemble label
and an empty `tii` list, because the channel SNR (about -5 dB) is too low for it. The module therefore has its
own numpy decoder. It uses the same carrier layout and pattern table as welle.io: both carriers of a pair carry
the PRS phase of the first carrier.

## Method (short)
* **Channelising.** FFT overlap-save at 20 Msps: one 1.25 M-point FFT per segment is shared by all blocks, and
  each block is cut out at exactly 2.048 Msps (ratio 64/625). This takes about 25 s for a 10 s, 400 MB capture.
* **Frame timing.** Each frame is timed on its own PRS correlation peak. The captures **drop samples**: in
  `dab_224` the frame spacing jumps by -129, -29, -17 and -12 samples at 2.048 Msps, and in `dab_207` there are
  more than 10 jumps. Both blocks of a capture jump at the same instants, so the jumps are USB drop-outs, not
  signal. A single straight-line frame clock fails.
* **Coherent averaging.** All frames are aligned to the average power-delay profile with sub-sample precision,
  and their phase is set by the main path. The null symbols are then averaged coherently. Over the 52 usable
  frames this reaches a -44 dB floor in the composite impulse response.
* **TII parity.** TII is sent only in **alternate frames**. The mean correlation is about 1.9 in one parity and
  about 0.1 in the other, so the module selects the TII parity, which gains 3 dB.
* **Detection and delay.** Codes are detected one at a time, strongest first, over all 24 x 70 codes using the
  coherent delay profile. The delay comes from the 8x-oversampled profile with parabolic interpolation.
* **Lobe choice.** A single code's 32 carriers give strong sidelobes every 5.33 samples and near-full grating
  lobes every 42.67 samples (6.25 km). The lobe is chosen by matching against the composite PRS impulse response,
  which has full bandwidth. Paths already owned by a stronger code are masked, and the other lobes are kept for
  the solver.
* **Solver.** Weighted Gauss-Newton on hyperbolic TDOA in a WGS84 local ENU frame around the transmitters'
  centroid, with 3-D distances. There is one unknown clock offset per ensemble, which is equivalent to
  differencing against a reference transmitter. It tries the lobe combinations allowed by
  |c·Δτ| ≤ baseline + 10 km. It rejects fixes with an accuracy worse than 20 km, and marks exactly-determined
  fixes (3 transmitters) as unverified with an accuracy of at least 1 km.

## Results on the 2026-10-30 captures (truth: Willemstad, about 51.69 N 4.44 E)
| capture / block | channel SNR | TII found (main-sub) | site | delay vs strongest |
|---|---|---|---|---|
| dab_224 11C (DAB+ 8008) | -7.0 dB | 21-20 (24.8 dB) | Roosendaal | 0 (reference) |
| dab_224 12C (NPO 8001) | -7.9 dB | 4-10 (22.6 dB) | Roosendaal | 0 (reference) |
| dab_207 9C (809C) | -4.6 dB | 19-05 (27.3 dB) | Rotterdam Anthony Fokkerweg | 0 (reference) |
| | | 19-20 (18.1 dB) | Roosendaal | +43.95 samples (+21.5 µs) |

The other blocks (11A, 11B, 11D, 12A VRT, 12B, 12D, 9A, 9B, 9D, 10A, 10B, ...) gave no PRS synchronisation, so
they are too weak with this antenna. The older 0.5 s captures (`iq_archive/.../dab_212`, `dab_226`) are too short:
5 frames, and no TII above the noise.

**Position fix: none.** At most 2 transmitters were identified in any single frequency network (SFN), and they
are the same 2 sites (Roosendaal and Rotterdam) in every ensemble. A hyperbolic fix needs at least 3 sites, so the
tool correctly prints `[]`. The SNR of these captures is the limit, not the method. The synthetic test at 0 dB SNR
finds 5/5 transmitters and puts the fix within 5 m.

Expected TII of the nearby NPO 12C transmitters, with the best SNR seen at any delay (noise maxima are about 8-11 dB):

| site | TII | best SNR |
|---|---|---|
| Dordrecht | 4-04 | 10.6 dB |
| Rotterdam | 4-05 | 10.6 dB |
| Breda | 4-03 | 9.1 dB |
| Bergen op Zoom | 4-16 | 7.5 dB |
| Goes | 4-11 | 8.0 dB |

So none of them is distinguishable from noise. In the composite (PRS) impulse response, the second strongest
path in 12C is at -47.8 samples (-14 dB). At that delay, Rotterdam's 4-05 gives 10.2 dB. That is consistent but
below the threshold, so it is not used.

### SFN offsets are not zero
For 9C, Roosendaal arrives 43.95 samples after Rotterdam. The database coordinates and the true location predict
-15.3 samples: Roosendaal is 18.5 km away and Rotterdam 20.8 km. That leaves a static transmitter offset of about
**+59 samples = +29 µs (8.7 km)**, Roosendaal relative to Rotterdam.

The tentative 12C observation (Rotterdam at -47.8) implies +62 samples = +30 µs, so the same offset appears in
two different networks. Network operators set such delays to shape SFN self-interference.

Effect on TDOA: even with enough transmitters, an uncalibrated fix can be wrong by kilometres. The database has an
`offset_us` field per transmitter, which `solve_with_ambiguity` subtracts. It is left null on purpose: the values
above were derived from the true location, and using them here would be circular.

## Transmitter database (`rfscan/data/dab_tx.json`)
77 ensembles, 398 transmitter records (378 with TII and coordinates). Coverage:
* NL national: 12C NPO, 11C DAB+, 7D MTVNL, 9C.
* NL regional and local layers.
* Flanders and Brussels: 12A VRT, 11A, 10D, 10A, 10B, 10C, 11D, 12B.

Fields: block, eid, label, site, lat, lon, tii_main, tii_sub, erp_kw, height_m, source, coord_source, confidence,
notes, offset_us. Sources, all accessed 2026-10-30:
* **antenneregister.nl.** The WFS of the Rijksinspectie Digitale Infrastructuur (`https://antenneregister.nl/mapserver/wfs/`,
  antenna groups with SAT_CODE=TDAB). It gives NL coordinates (RD to WGS84, about 1 m), block and antenna height.
  It has no TII.
* **radio-tv-nederland.nl DAB overview pages.** Per-site TII and ERP for 12C, 11C, 7D and 9C, plus the local
  allotments. This is the primary NL TII source.
* **wohnort.org "DAB Ensembles Worldwide".** The Netherlands and Belgium pages give EIds, labels and partial TII.
  Used for cross-checks.
* **fmtvdx.eu.** NL MyMaps lists, a DX log and the "DAB+ België" page (dated 2025-07-02). This is the primary
  Belgian TII source.
* **Belgian mast coordinates.** OpenStreetMap (Overpass) and Wikipedia for the major masts. Many Belgian entries
  are only **town centroids** (confidence "low"/"medium"), which is not good enough for TDOA.

Known conflicts are recorded in `notes`:
* The 12C Zeeland TII codes (Goes, Biervliet, Westdorpe) are swapped between sources.
* MTVNL Loon op Zand: 13/4 vs 13/14.
* 11C Wormer: 21/7 vs 21/2.
* Capelle: 36/5 vs 36/3.

fmscan.org needs a login, and the TII databases in AbracaDABra and Qt-DAB are closed, so neither was used.

## Open issues / next steps
1. **SNR.** Captures at -5 to -8 dB channel SNR show only the dominant transmitter, plus a second one in 9C.
   Options: an outdoor Band III antenna, more LNA gain (the captures used LNA 40 / VGA 30 / amp 0, so the
   front-end amp is off), or a longer capture. coherent TII averaging gains 3 dB per doubling of frames (while the channel stays static):
   30 s ≈ +4.8 dB, 60 s ≈ +7.8 dB.
2. **Sample drops.** USB drop-outs inside a frame are not detected; such frames only add noise. Capturing at
   10 Msps, or 2.048 Msps per block, would cut USB load and drops. A dedicated `capture` preset for TDOA would
   help.
3. **Static SFN offsets** must be calibrated before positioning works, either from a survey at a known location
   or from operator data. Fill `offset_us` per site and ensemble, then validate at a second location.
4. **Multipath.** The delay is the strongest path of each TII, not the first arrival. NLOS bias is not handled.
5. **Belgian coordinates** need real mast positions (BIPT has no open register; OSM tags are incomplete).
6. **Ambiguity.** Grating and sidelobe ambiguity (5.33 / 42.7 samples) is resolved with the composite impulse
   response. With only 3 transmitters (an exactly-determined fix) a wrong lobe cannot be detected, which is why
   such fixes are marked "unverified".
