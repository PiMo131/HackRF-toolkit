"""Netherlands / CEPT band plan used to label detections and pick decoders.

Each entry: (f_lo_MHz, f_hi_MHz, service, category, decoders, priority)
`decoders` names map to modules in rfscan.decoders. Priority 0..3: higher
means the scheduler dwells on it sooner / longer.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    lo: float
    hi: float
    service: str
    category: str
    decoders: tuple = ()
    priority: int = 1


BANDS = [
    # --- LF / HF ---------------------------------------------------------
    Band(0.0, 0.3, "LF (time signals DCF77 77.5k, navigation)", "lf"),
    Band(0.3, 3.0, "MF (AM broadcast, maritime, NDB)", "broadcast"),
    Band(3.0, 30.0, "HF (shortwave broadcast, amateur, aero/maritime HF, OTH)", "hf"),
    Band(26.96, 27.41, "CB radio 27 MHz", "voice", ("fm_audio",)),
    Band(27.0, 27.3, "27 MHz SRD (old RC, alarms)", "ism", ("rtl433",), 2),
    Band(30.0, 50.0, "VHF low (military, old PMR)", "voice"),
    Band(50.0, 52.0, "Amateur 6 m", "amateur"),
    Band(68.0, 87.5, "VHF mobile / military", "voice"),
    Band(87.5, 108.0, "FM broadcast", "broadcast", ("wbfm",), 0),
    Band(108.0, 118.0, "Aero navigation (VOR/ILS)", "aero"),
    Band(118.0, 137.0, "Airband voice (AM)", "aero", ("am_audio",), 1),
    Band(131.525, 131.725, "ACARS", "aero", (), 2),
    Band(137.0, 138.0, "Weather satellites (NOAA APT/Meteor LRPT)", "satellite", (), 1),
    Band(144.0, 146.0, "Amateur 2 m (APRS 144.800)", "amateur", ("afsk_aprs",), 1),
    Band(146.0, 156.0, "VHF mobile (PMR, business, rail)", "voice"),
    Band(156.0, 162.05, "Marine VHF", "maritime", ("fm_audio",), 1),
    Band(161.95, 162.05, "AIS (161.975 / 162.025)", "maritime", ("ais",), 3),
    Band(169.4, 169.475, "169 MHz wM-Bus (smart meters mode N)", "ism", ("rtl433",), 2),
    Band(169.6, 169.7, "P2000 FLEX paging (169.650)", "paging", ("pager",), 3),
    Band(169.8, 174.0, "VHF paging / PMR", "paging", ("pager",), 2),
    Band(174.0, 240.0, "DAB+ digital radio (Band III)", "broadcast", ("dab",), 1),
    Band(240.0, 380.0, "Military UHF / aero / satcom", "military"),
    Band(380.0, 400.0, "C2000 TETRA (emergency services, encrypted)", "tetra", ("tetra_detect",), 1),
    Band(400.0, 406.0, "Radiosondes / Meteo", "sensor", (), 1),
    Band(406.0, 406.1, "COSPAS-SARSAT EPIRB", "beacon", (), 2),
    Band(410.0, 430.0, "UHF PMR / business / rail", "voice"),
    Band(430.0, 440.0, "Amateur 70 cm", "amateur"),
    Band(433.05, 434.79, "ISM 433 MHz (sensors, remotes, LoRa)", "ism", ("rtl433", "lora"), 3),
    Band(446.0, 446.2, "PMR446 walkie-talkies", "voice", ("fm_audio",), 2),
    Band(450.0, 470.0, "UHF business / PAMR / CDMA450", "voice"),
    Band(470.0, 694.0, "DVB-T2 terrestrial TV / PMSE", "broadcast", (), 0),
    Band(703.0, 733.0, "LTE/NR band 28 uplink", "cellular_ul", (), 1),
    Band(758.0, 788.0, "LTE/NR band 28 downlink (700 MHz)", "cellular", ("lte", "nr"), 2),
    Band(791.0, 821.0, "LTE band 20 downlink (800 MHz)", "cellular", ("lte",), 2),
    Band(832.0, 862.0, "LTE band 20 uplink", "cellular_ul", (), 1),
    Band(863.0, 870.0, "SRD 868 (LoRaWAN EU868, sensors, alarms, wM-Bus)", "ism", ("rtl433", "lora"), 3),
    Band(868.7, 869.2, "868.95 wM-Bus mode T/C (smart meters)", "ism", ("rtl433",), 3),
    Band(870.0, 876.0, "SRD / RFID 870-876", "ism", ("rtl433",), 1),
    Band(876.0, 880.0, "GSM-R uplink", "rail"),
    Band(880.0, 915.0, "GSM/UMTS/LTE band 8 uplink (900)", "cellular_ul", (), 1),
    Band(915.0, 921.0, "RFID 915-921 / SRD", "ism", (), 1),
    Band(921.0, 925.0, "GSM-R downlink (railways)", "rail", ("gsm",), 2),
    Band(925.0, 960.0, "GSM/UMTS/LTE band 8 downlink (900)", "cellular", ("gsm", "lte"), 3),
    Band(960.0, 1215.0, "Aero DME/TACAN, SSR", "aero"),
    Band(1087.0, 1093.0, "ADS-B / Mode S 1090", "aero", ("adsb",), 3),
    Band(1029.0, 1031.0, "SSR interrogations 1030", "aero"),
    Band(1164.0, 1300.0, "GNSS L5/E5/L2/E6", "gnss"),
    Band(1240.0, 1300.0, "Amateur 23 cm / radar", "amateur"),
    Band(1427.0, 1518.0, "L-band SDL (LTE band 32/75)", "cellular", ("lte",), 1),
    Band(1525.0, 1559.0, "Inmarsat / Thuraya downlink", "satellite"),
    Band(1559.0, 1610.0, "GNSS L1/E1/B1/G1", "gnss"),
    Band(1616.0, 1626.5, "Iridium", "satellite", (), 1),
    Band(1710.0, 1785.0, "GSM/LTE band 3 uplink (1800)", "cellular_ul", (), 1),
    Band(1805.0, 1880.0, "GSM/LTE band 3 downlink (1800)", "cellular", ("gsm", "lte"), 3),
    Band(1880.0, 1900.0, "DECT cordless phones", "dect", ("dect_detect",), 2),
    Band(1920.0, 1980.0, "UMTS/LTE/NR band 1 uplink (2100)", "cellular_ul", (), 1),
    Band(2110.0, 2170.0, "UMTS/LTE/NR band 1 downlink (2100)", "cellular", ("lte", "nr"), 3),
    Band(2400.0, 2483.5, "ISM 2.4 GHz (Wi-Fi, BLE, Zigbee, drones)", "ism", ("ofdm", "ble"), 2),
    Band(2500.0, 2570.0, "LTE band 7 uplink (2600)", "cellular_ul", (), 1),
    Band(2570.0, 2620.0, "LTE band 38 TDD (2600)", "cellular", ("lte",), 1),
    Band(2620.0, 2690.0, "LTE band 7 downlink (2600)", "cellular", ("lte",), 3),
    Band(2700.0, 3100.0, "Radar (aero/met)", "radar"),
    Band(3300.0, 3800.0, "5G NR n78 (3.5 GHz)", "cellular", ("nr", "ofdm"), 3),
    Band(5150.0, 5350.0, "Wi-Fi 5 GHz UNII-1/2", "wifi", ("ofdm",), 1),
    Band(5470.0, 5725.0, "Wi-Fi 5 GHz UNII-2e (DFS)", "wifi", ("ofdm",), 1),
    Band(5725.0, 5875.0, "ISM 5.8 GHz (FPV video, Wi-Fi, DSRC)", "ism", ("ofdm",), 2),
    Band(5875.0, 5925.0, "ITS-G5 / C-V2X 5.9 GHz", "its", ("ofdm",), 1),
]


def lookup(f_hz):
    """All matching bands for a frequency, narrowest (most specific) first."""
    f = f_hz / 1e6
    hits = [b for b in BANDS if b.lo <= f < b.hi]
    return sorted(hits, key=lambda b: b.hi - b.lo)


def best(f_hz):
    hits = lookup(f_hz)
    return hits[0] if hits else Band(0, 0, "unallocated / unknown", "unknown", (), 1)


def decoders_for(f_lo_hz, f_hi_hz):
    """Union of decoders for any band overlapping [f_lo, f_hi]."""
    lo, hi = f_lo_hz / 1e6, f_hi_hz / 1e6
    out = []
    for b in sorted(BANDS, key=lambda b: b.hi - b.lo):
        if b.lo < hi and b.hi > lo:
            for d in b.decoders:
                if d not in out:
                    out.append(d)
    return out
