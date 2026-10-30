"""Generic hyperbolic TDOA positioning.

* WGS84 lat/lon/height <-> local ENU (east, north, up) around a reference point.
* Gauss-Newton weighted least squares on time differences of arrival.  The receiver
  clock offset is eliminated by differencing every arrival time against a reference
  transmitter, so only the 2-D receiver position (receiver height fixed) is estimated.
  Needs >= 3 transmitters (2 differences for 2 unknowns; then the fit is exactly determined,
  there is no redundancy and a mirror solution may exist; >= 4 gives a residual check).
* The covariance of the solution, propagated from the timing error model, gives an accuracy
  in metres (DRMS, ~65 % radius).

Timing error model: each transmitter's arrival time has an independent error with
sigma_i^2 = sigma_meas_i^2 + sigma_sync^2 (sigma_sync: transmitter-to-GPS phase error, for
TS 38.133 cell phase synchronisation +-1.5 us max -> ~0.5 us 1-sigma is a fair default).
Differencing against the reference makes the errors correlated: C = diag(s_i^2) + s_ref^2 * 1 1^T.
"""
import math

import numpy as np

C_LIGHT = 299792458.0
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def geodetic_to_ecef(lat, lon, h=0.0):
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return np.array([(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
                     (n * (1 - _E2) + h) * math.sin(la)])


def ecef_to_geodetic(x, y, z):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _E2))
    h = 0.0
    for _ in range(8):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def _rot(lat0, lon0):
    la, lo = math.radians(lat0), math.radians(lon0)
    return np.array([[-math.sin(lo), math.cos(lo), 0],
                     [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
                     [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]])


def to_enu(lat, lon, h, lat0, lon0, h0=0.0):
    d = geodetic_to_ecef(lat, lon, h) - geodetic_to_ecef(lat0, lon0, h0)
    return _rot(lat0, lon0) @ d


def from_enu(e, n, u, lat0, lon0, h0=0.0):
    x = geodetic_to_ecef(lat0, lon0, h0) + _rot(lat0, lon0).T @ np.array([e, n, u], float)
    return ecef_to_geodetic(*x)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- solver
def _model(p2, S, pairs, h_rx):
    """Range differences |p - s_i| - |p - s_ref| (m) for (i, ref) pairs and the Jacobian w.r.t. (e, n)."""
    p = np.array([p2[0], p2[1], h_rx])
    d = S - p
    r = np.linalg.norm(d, axis=1)
    u = -d / r[:, None]           # d r_i / d p
    a, b = pairs[:, 0], pairs[:, 1]
    return r[a] - r[b], (u[a] - u[b])[:, :2]


def solve(tx, ref=None, sigma_sync_s=0.5e-6, h_rx=1.5, origin=None, grid_km=25.0, grid_step_m=500.0, iters=30):
    """Hyperbolic TDOA fix.

    tx: list of dicts {lat, lon, [h], t (arrival time, s, any common offset), [sigma] (s), [group]}.
    Arrival times are only compared within a group (one group per capture: each capture has its own
    unknown receiver clock offset); every group is differenced against its own reference.
    ref: index of the reference transmitter for a single group (default: smallest sigma in the group).
    Returns dict(lat, lon, east_m, north_m, acc_m, cov_m2 (2x2), resid_m (list), rms_ns, n, ref, ambiguous, solutions)
    or None with fewer than 3 transmitters.  `solutions` lists distinct local minima (lat, lon, cost)
    found by the start grid, so callers can report mirror/ambiguous fixes honestly."""
    if len(tx) < 3:
        return None
    groups = {}
    for i, t in enumerate(tx):
        groups.setdefault(t.get("group", 0), []).append(i)
    if origin is None:
        origin = (float(np.mean([t["lat"] for t in tx])), float(np.mean([t["lon"] for t in tx])))
    lat0, lon0 = origin
    S = np.array([to_enu(t["lat"], t["lon"], t.get("h", 25.0), lat0, lon0) for t in tx])
    tt = np.array([float(t["t"]) for t in tx])
    sig = np.array([math.hypot(float(t.get("sigma", 0.0) or 0.0), sigma_sync_s) for t in tx])
    pairs, refs = [], []
    for gk, mem in groups.items():
        if len(mem) < 2:
            continue
        r0 = ref if (ref is not None and ref in mem) else mem[int(np.argmin(sig[mem]))]
        refs.append(r0)
        pairs += [(i, r0) for i in mem if i != r0]
    if len(pairs) < 2:
        return None
    pairs = np.array(pairs, int)
    z = (tt[pairs[:, 0]] - tt[pairs[:, 1]]) * C_LIGHT        # measured range differences (m)
    Cm = np.diag(sig[pairs[:, 0]] ** 2)
    same = pairs[:, 1][:, None] == pairs[:, 1][None, :]
    Cm = (Cm + same * (sig[pairs[:, 1]] ** 2)[:, None]) * C_LIGHT ** 2
    W = np.linalg.inv(Cm)

    def cost(p2):
        f, _ = _model(p2, S, pairs, h_rx)
        v = z - f
        return float(v @ W @ v)

    def gn(p2):
        p2 = np.array(p2, float)
        lam = 1e-3
        c = cost(p2)
        for _ in range(iters):
            f, J = _model(p2, S, pairs, h_rx)
            v = z - f
            A = J.T @ W @ J
            g = J.T @ W @ v
            try:
                dp = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), g)
            except np.linalg.LinAlgError:
                break
            pn = p2 + dp
            cn = cost(pn)
            if cn < c:
                p2, c, lam = pn, cn, max(lam / 10, 1e-9)
                if np.linalg.norm(dp) < 0.01:
                    break
            else:
                lam *= 10
                if lam > 1e8:
                    break
        return p2, c

    # coarse start grid -> local minima -> Gauss-Newton (Levenberg-Marquardt damped)
    g = np.arange(-grid_km * 1e3, grid_km * 1e3 + 1, grid_step_m)
    E, N = np.meshgrid(g, g)
    P = np.stack([E.ravel(), N.ravel(), np.full(E.size, h_rx)], 1)
    R = np.linalg.norm(P[:, None, :] - S[None, :, :], axis=2)
    F = R[:, pairs[:, 0]] - R[:, pairs[:, 1]]
    V = z[None, :] - F
    Cg = np.einsum("ij,jk,ik->i", V, W, V).reshape(E.shape)
    starts = []
    for k in np.argsort(Cg.ravel())[:400]:
        i, j = divmod(int(k), Cg.shape[1])
        win = Cg[max(i - 1, 0):i + 2, max(j - 1, 0):j + 2]
        if Cg[i, j] <= win.min():
            starts.append((E[i, j], N[i, j]))
        if len(starts) >= 6:
            break
    sols = []
    for s0 in starts or [(0.0, 0.0)]:
        p2, c = gn(s0)
        if all(np.linalg.norm(p2 - q) > 50 for q, _ in sols):
            sols.append((p2, c))
    sols.sort(key=lambda s: s[1])
    p2, c = sols[0]
    f, J = _model(p2, S, pairs, h_rx)
    resid = z - f
    try:
        cov = np.linalg.inv(J.T @ W @ J)
    except np.linalg.LinAlgError:
        cov = np.full((2, 2), np.inf)
    dof = len(z) - 2
    if dof > 0:
        s2 = c / dof                          # a-posteriori variance factor: inflate if residuals are large
        cov = cov * max(1.0, s2)
    acc = float(math.sqrt(max(np.trace(cov), 0))) if np.all(np.isfinite(cov)) else float("inf")
    lat, lon, _ = from_enu(p2[0], p2[1], h_rx, lat0, lon0)
    out_sols = []
    for q, cq in sols:
        la, lo, _ = from_enu(q[0], q[1], h_rx, lat0, lon0)
        out_sols.append(dict(lat=la, lon=lo, cost=float(cq)))
    # ambiguous when another minimum fits (nearly) as well (exactly-determined 3-site case)
    amb = len(sols) > 1 and sols[1][1] < max(4.0, 2 * sols[0][1] + 1.0)
    return dict(lat=lat, lon=lon, east_m=float(p2[0]), north_m=float(p2[1]), acc_m=acc, cov_m2=cov.tolist(),
                resid_m=[float(v) for v in resid], rms_ns=float(np.sqrt(np.mean(resid ** 2)) / C_LIGHT * 1e9),
                cost=float(c), n=len(tx), refs=refs, dof=dof, ambiguous=bool(amb), solutions=out_sols)


def solve_assignments(cells, candidates, max_combos=200000, **kw):
    """Try PCI -> site assignments and keep the one with the smallest weighted residual.

    cells: list of {key, t, sigma, [operator]}; candidates: {key: [ {lat, lon, h, site, ...}, ...]} or
    candidates per operator via cell['cands'].  Only meaningful with >= 4 cells (3 cells always fit
    exactly).  Distinct cells must map to distinct sites unless they share a site (same mast sectors),
    which is allowed.  Returns (best_solution, assignment list, n_tried) or (None, None, n)."""
    import itertools
    lists = [c.get("cands") or candidates.get(c["key"], []) for c in cells]
    if any(not l for l in lists):
        return None, None, 0
    best = (None, None)
    n = 0
    for combo in itertools.product(*lists):
        n += 1
        if n > max_combos:
            break
        tx = [dict(lat=s["lat"], lon=s["lon"], h=s.get("h", 25.0), t=c["t"], sigma=c.get("sigma", 0.0))
              for c, s in zip(cells, combo)]
        sol = solve(tx, grid_step_m=1000.0, **kw)
        if sol is None:
            continue
        if best[0] is None or sol["cost"] < best[0]["cost"]:
            best = (sol, list(combo))
    return best[0], best[1], n
