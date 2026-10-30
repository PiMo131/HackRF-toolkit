# NR SSB TDOA positioning: notes (2026-10-30)

## What was built
* `rfscan/decoders/nr.py` (opt-in only; default `decode()` output was checked byte-identical against the old module on the synthetic SSB):
  * New `ctx` keys: `timing`, `usable` (default 0.75) and `det_seconds` (default 0.06).
  * `ctx["timing"]=True` adds `fields["timing"]`: per occurrence it gives the sub-sample PSS time, the SSB index and the implied half-frame start. It also adds `fields["co_channel"]`: further N_ID1 hypotheses that share the PSS.
  * Sub-sample time comes from two estimators. `t_pss_parabola_s` is a parabolic fit of the PSS+SSS correlation peak. `t_pss_s` is the frequency-domain ML delay (`fine_delay`), i.e. the phase slope fitted across all 127 subcarriers. The adjacent-subcarrier phase-slope estimator was tried first and was far too noisy (several samples at 0 dB per-subcarrier SNR), so it was dropped.
  * SSB position: case C, `ssb_start_symbols` gives {2,8}+14n; `symbol_useful_start_s` follows TS 38.211 CP lengths, with the extra 16κ on the first symbol of each 0.5 ms. The SSB index comes from PBCH DMRS. Occurrences that DMRS cannot resolve take the candidate consistent with the cell's other SSBs. If no SSB of the cell is resolved, they take the candidate closest to the reference cell's frame (`issb_by_sync`, a sync assumption, flagged).
  * The half-frame bit needs the MIB, which is not decoded. Frame starts are therefore known modulo 5 ms. This is harmless for synchronised cells.
  * Periodicity per cell is the spacing of same-index occurrences (5/10/20/40 ms).
  * `cancel_cell`: when timing is on, cells already found are subtracted (PSS+SSS) before the SSS of weaker cells on the same SSB frequency is searched. Synchronised cells send their SSBs at the same instant. Without this step the 2nd and 3rd synthetic cells were not found at all (SSS z 2.4 and 3.0; with it, 10.8 and 9.0).
* `rfscan/tdoa.py`:
  * WGS84 ↔ ENU conversion.
  * Levenberg-damped Gauss-Newton WLS on TDOAs, using a start grid that also finds mirror minima.
  * Receiver clock offset removed by differencing per *group*: one group per capture, since each capture has its own clock.
  * Error model: per-cell timing σ plus gNB sync σ (default 0.5 µs); the differencing correlation is in the covariance.
  * Accuracy is reported as DRMS, inflated by the a-posteriori variance when dof > 0.
  * Needs at least 3 sites. With exactly 3 there is no residual check and the result is flagged.
* `rfscan/tdoa_nr.py`: CLI `python -m rfscan.tdoa_nr caps... [--sites json] [--lte] [--assign] [--sync-us] [--json]`.
  * The reference cell is the one with the highest PSS SNR. Each cell is compared with the reference's frame-start series interpolated at the cell's own times. This cancels receiver clock drift *and wander* (see below).
  * Occurrences in the first 0.15 s are skipped (HackRF start-up transient).
  * `--assign` tries every PCI→mast assignment over the operator's candidate masts (operator taken from the SSB frequency) and keeps the minimum residual. It needs at least 4 cells. The candidate list is centred on the survey point, which is a weak prior.
  * LTE mode: full-capture 5 ms folding search, then per-cell PSS timing and 10 ms frame starts (the SSS tells subframe 0 from subframe 5).
* `rfscan/data/nr_sites_willemstad.json`:
  * Source: Antenneregister WFS, `https://antenneregister.nl/mapserver/wfs/` (EPSG:28992 only; layers `ms:Antennes` + `Antennes_Groepen`).
  * Content: 67 n78 masts within 15 km. Operator comes from the registered centre frequency 3500/3600/3700 MHz → VodafoneZiggo 3450–3550 / Odido 3550–3650 / KPN 3650–3750 (2024 auction, Kamerstuk 24095-587). That attribution was cross-checked against each site's other bands.
  * The register has no PCIs, so there are no PCI keys yet, only `_candidates`.
  * Nearest masts: 260 m away (KPN + Odido, Willemstad, 4797AA) and 1.9 km (KPN, 4797SG).
* `tests/tdoa/test_tdoa_nr.py` (plain script or pytest, ~9 s):
  * Synthetic: 3 cells at +0.40 / +3.77 / −1.13 µs with different beams. Offsets were recovered with errors of 5, 2 and 0.1 ns; SSB indices were correct.
  * Solver: 4 sites gives about 10 m error at 30 ns noise. 5 sites in 2 capture groups gives about 13 m.
  * Real-data smoke test on a 0.2 s slice of `lte_b7_2660` (PCI 213).

## Results on real data
### n78, 2026-10-30 (3465 / 3545 / 3575, 2 s each)
**No SSB was found in any of the three captures**:
* `nr.decode` with 0.75 or 0.9 usable bandwidth found nothing.
* A drift-tolerant 2 s fold over the full 10 kHz grid (`scratchpad longsearch`, about 5 min per capture) also found nothing. Peak/median was 3.8–4.5 (noise level), against more than 20 expected for a real PSS.

What is in the captures:
* 3575: noise, plus about 1 MHz-wide 0.5 ms bursts every 5 ms at 3580.0–3581.2 MHz. This looks like UE uplink (TDD UL slots), not SSB.
* 3465: a 4 MHz-wide bursty signal at 3455.5–3459.6 MHz (0.5 ms slots in a 5 ms pattern) with no PSS. Probably UE uplink or PDSCH, not an SSB.
* 3545: noise.

So the operators' SSBs lie outside the captured windows:
* Odido block 3550–3650: SSB probably near 3609 MHz. The 10-19 findings list PCI 976 @ 3609.12, but the current decoder cannot reproduce it from `reference_captures/n78_3610`.
* KPN block 3650–3750: not captured at all.
* The 0.1 s reference sweep 3410–3790 (10-19) also gives no SSB with the current decoder.

**Hence no NR offsets and no NR fix.**

To get one:
1. Find the SSB frequencies of all three operators, for example with 20 MHz steps and 2 s dwells, and `nr.decode(..., {"usable":0.9, "det_seconds":0.5})`.
2. Capture 2 s centred on each SSB.
3. Each capture should then show the serving PCI plus neighbours of the *same operator* (same SSB ARFCN).
4. TDOA needs at least 3 PCIs inside one capture, or at least 2 differences spread over captures.

### LTE frame offsets (`--lte`)
2026-10-30 captures: only one real cell per capture, so they give no inter-cell test.

| Capture | Cell | Note |
|---|---|---|
| B20 815 | PCI 417 @ 816 | Also PCI 417 @ 811, 6 dB weaker, std 1.7 µs; probably a spur or image |
| B3 1815 | PCI 420 | |
| B7 2660 | PCI 213 | |
| B32 1474.5 | PCI 318 | |
| B8 950 | none | Even `lte.decode` finds nothing |

Single-cell PSS timing noise: 0.06–0.13 µs per PSS at good SNR, 0.47 µs on B20.

**HackRF clock**: the "receiver drift" estimated from the reference cell varies between captures taken seconds apart: −0.23, −0.36, +1.43 and +5.9 ppm. B3 showed a wander of about 8 µs in the first ~0.1 s, and B20 a 25 ms periodic ±1 µs pattern. The sample clock is not stable at the µs level over a capture. That is why offsets are measured against the reference cell's *interpolated* time series, not a line fit, and why only cells within one capture can be compared.

Older captures (`iq_archive/2026-10-19_antwerp/reference_captures`) show different PCIs than 10-30: 816 → 291, 1815 → 255, 950 → 277, 940 → 414. Treat them as possibly another location or another time.
* `lte800_806` (VodafoneZiggo B20): PCI 334 (ref) and PCI 497 on the same carrier. PCI 497 is offset by **+1.23 µs**, std 0.13 µs over 27 half-frames in 0.3 s (−1.22 µs with 497 as reference). The two cells have different N_ID1, so they are probably different sites. A µs-level, stable offset that is ≪ 5 ms is what phase-synchronised (GPS/PTP) FDD eNBs look like; unsynchronised FDD cells sit at arbitrary offsets within the 10 ms frame. This is one pair in one 0.3 s capture: suggestive, not proof.
* No other old capture holds two cells at once.

## Open issues
* Main blocker: NR SSB frequencies are unknown and not captured. The next step is above.
* PCI→site mapping does not exist in public data. Options: assignment search (≥4 cells, low confidence); the strongest cell is likely the 260 m Willemstad mast; drive-around or power-gradient methods would confirm.
* Multipath bias: the delay estimator returns the dominant path, not the first path. NLOS adds positive bias of up to µs.
* The MIB/half-frame bit is not decoded, so offsets are modulo 5 ms (10 ms for LTE). This is fine for synchronised networks.
* LTE search over 1 s is slow (about 75 s per capture on 24 threads).
* `nr.decode` `occurrences` counts duplicate PSS positions. This is existing behaviour, left unchanged; timing de-duplicates.
