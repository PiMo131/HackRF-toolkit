# Offline cell-tower lookup for "Where am I?" (2026-10-30)

The beaconDB geolocate call in the browser is blocked by CORS (`whereCells()` in web/index.html). This adds a
lookup that runs fully offline, from a local copy of a public cell-tower database.

## Data source
* **beaconDB**: no public data export yet. beacondb.net says "data dumps currently aren't available", they are
  planned as public-domain dumps later. For cells, beaconDB itself falls back to the MLS final dump.
* **Used: the Mozilla Location Service final full cell export**, mirrored on archive.org (item
  `MLS_Full_Cell_Export_Final`, file `MLS-full-cell-export-final.csv.gz`, 1.57 GB, public domain / CC0, dated
  2024-03). It is in the `cell_towers` format. No account is needed:
  `curl -L -O https://archive.org/download/MLS_Full_Cell_Export_Final/MLS-full-cell-export-final.csv.gz`
* **OpenCelliD** works with the same converter (its per-MCC files have no header line; the converter handles that). It needs a
  free API token: `https://opencellid.org/ocid/downloads?token=<TOKEN>&type=mcc&file=204.csv.gz`.
  This data is CC BY-SA 4.0, so it needs attribution. It would add towers built after 2024.

## Files
| file | content | size |
|---|---|---|
| `web/cells/204.csv.gz` | NL, 80 562 GSM cells | 1.1 MB |
| `web/cells/206.csv.gz` | BE, 65 352 GSM cells | 0.9 MB |
| `web/cells/262.csv.gz` | DE, 701 814 GSM cells | 9.7 MB |

The columns are `radio,net,area,cell,lon,lat,range,samples`, with positions rounded to 1e-5° (about 1 m). Each file is under 15 MB, so
they are not in .gitignore. With all radio types the files would be 7.5 / 4.4 / 43.2 MB (204 has 216k LTE and 250k UMTS cells). The default is
GSM only, because GSM is the only radio we can match (see LTE below).

Rebuild: `python -m rfscan.cellpos convert MLS-full-cell-export-final.csv.gz [--mcc 204,206,262] [--radio GSM|GSM,LTE|all]`
(this takes about 2.7 min). Run it with `python -I rfscan/cellpos.py convert ...` when the input is a fresh download.

## Lookup (rfscan/cellpos.py, same logic in web/index.html)
1. Merge duplicate cells (mcc, mnc, lac, ci) and use the mean power_db.
2. Look up the exact GSM cell by (mnc, lac, ci) and use its range, with a 2 km floor. MLS `range` is the spread of the observations, typically
   15–30 km for the cells here. There is no cross-match with UMTS or LTE, because their cell IDs use a different 28-bit space.
3. If the CI is unknown, use the LAC fallback: the centroid of all GSM cells of that LAC, with the 90th-percentile distance as range.
4. Estimate: a weighted mean, weight = 10^((P−Pmax)/20) / range. **LAC centroids are only used when no cell was found by CI.**
   Mixing them in moved the Willemstad estimate 14 km south (51.565, 4.420). The two unknown KPN cells are the strongest, and LAC 3350
   spans about 19 km, so the strong-signal weight pulled the result towards a meaningless centroid.
5. Accuracy: weighted RMS of (tower-to-estimate distance)² + (range/√2)², with a 3 km floor. The receiver lies within roughly one
   range of every tower it hears. R/√2 is the RMS radius of a uniformly covered disc. This is conservative.

CLI: `python -m rfscan.cellpos runs/<run>/rfscan.sqlite [-v]`. It also accepts a JSON list of `{mcc,mnc,lac,ci,power_db}`, or a
geolocate-API `{"cellTowers":[...]}`. Output: `[{"src":"celldb","lat","lon","acc","n","note"}]`. Without a cell file it reports
"no cell file" and exits with code 1.

Browser: the checkbox "cell DB (offline)" (`data-src="celldb"`, on by default) loads `cells/<mcc>.csv.gz` on demand with
fetch + DecompressionStream. Each MCC loads once and stays cached in memory. It also accepts a file the server has already
un-gzipped. The text field "extra estimates JSON" takes a pasted list (or a single object) of estimates, such as the CLI output above.
Each entry is added to the estimates and to the fused result.

## Validation (truth: Willemstad NB, about 51.69 N 4.44 E)
| input | cells | estimate | ± | error |
|---|---|---|---|---|
| runs/20261029_104759/rfscan.sqlite | 3 by CI (204-04 21751, 21752; 204-08 48694) + 2 unknown | 51.68799, 4.43574 | 14.7 km | **0.37 km** |
| runs/locate_20261030/gsm_937 (analysed again with `rfscan analyze`) | 2 by CI (21752, 48694) + 2 unknown | 51.68521, 4.43775 | 15.9 km | **0.56 km** |

KPN 204-08 LAC 3350 CI 48694 is in MLS at 51.679, 4.442, about 1.2 km from the truth. CIs 56061 and 56062 (the strongest cells) are not in
MLS. They are probably newer than 2024. The stated ± is about 30× the real error. It is honest given what one cell ID can tell you, but loose here.
For comparison, beaconDB gave 51.709, 4.421 ±32 km from the Vodafone cell only.

Tests: `.venv/bin/python tests/cells/test_cellpos.py` (synthetic CSV) and `node tests/web/test_celldb.js` (JS lookup
from the index.html block, with a fake fetch; it gives the same estimate as Python on the real 204 file).

## LTE / NR: not matchable yet
Cell databases key LTE cells by ECI (eNB ID × 256 + sector), together with TAC. Our LTE decoder gives PCI + EARFCN + MIB only.
PCI is just 0–503 and is reused everywhere, so LTE cells cannot be looked up. The follow-up is **SIB1 decoding** (PDCCH/SI-RNTI,
then PDSCH), which carries the cellIdentity (28-bit ECI) and the TAC. After that, rebuild the files with `--radio GSM,LTE`. That is about +3 MB for 204,
and the 262 file would need splitting or LTE-only on demand. The same applies to NR (NCI is in SIB1, and NR MIB is not decoded yet).

## Open issues
* MLS stopped in 2024: new sites, and refarmed or renumbered cells, are missing (here 2 of 4 KPN cells). Adding OpenCelliD (token)
  would fill gaps. beaconDB dumps can replace MLS once published.
* The accuracy is conservative because MLS `range` is large. A disc-intersection (all towers heard ⇒ inside every disc)
  would give a tighter and still honest bound.
* Only 204/206/262 are converted. Other MCCs need `--mcc`. The browser reports "no cells/<mcc>.csv.gz" for other countries.
* `web/build.sh` will copy cellpos.py into web/py as well (it is harmless, and the browser uses the JS version).
