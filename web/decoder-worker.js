// Runs the rfscan Python analysis pipeline and decoders in the browser via Pyodide (WebAssembly).
// The Python sources come from web/py (copied there by build.sh), so results match the native tool.
// Decoders that call native programs (multimon-ng, rtl_433) get those programs as WebAssembly builds
// from web/tools (made by build_tools.sh); their subprocess calls are routed to runTool() below.
"use strict";
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.27.2/full/';
importScripts(PYODIDE + 'pyodide.js');

// pure numpy/scipy decoders, always available
const PURE_DECODERS = ['adsb', 'ais', 'ble', 'gsm', 'lora', 'lte', 'nr'];
// decoder -> WebAssembly tool it needs (emscripten MODULARIZE factory in tools/<file>.js)
const TOOL_DECODERS = { pager: 'multimon-ng', rtl433: 'rtl_433' };
const TOOLS = { 'multimon-ng': { file: 'multimon-ng', factory: 'create_multimon' }, 'rtl_433': { file: 'rtl_433', factory: 'create_rtl433' } };
const toolWasm = {};
let py = null, started = null;

async function loadTools() {
  for (const [name, t] of Object.entries(TOOLS)) {
    try {
      const r = await fetch(`tools/${t.file}.wasm`);
      if (!r.ok) continue;
      importScripts(`tools/${t.file}.js`);
      if (typeof self[t.factory] !== 'function') continue;
      toolWasm[name] = await r.arrayBuffer();
    } catch (err) { /* tool not built: its decoder stays native-only */ }
  }
}

// Synchronous: Python calls this from inside a decoder. Each call gets a fresh module instance
// (the C programs keep global state), compiled synchronously from the cached wasm bytes.
function runTool(name, args, files) {
  args = Array.from(args); files = Array.from(files);
  const t = TOOLS[name];
  if (!t || !toolWasm[name]) return { code: 127, stdout: '', stderr: `${name}: not available in the browser` };
  const out = [], err = [];
  const M = { wasmBinary: toolWasm[name], noInitialRun: true, print: l => out.push(l), printErr: l => err.push(l) };
  self[t.factory](M);
  if (typeof M.callMain !== 'function') return { code: 126, stdout: '', stderr: `${name}: module did not initialise synchronously` };
  for (const p of files) {
    const dir = p.split('/').slice(0, -1).join('/');
    if (dir) M.FS.mkdirTree(dir);
    M.FS.writeFile(p, py.FS.readFile(p));
  }
  let code = 0;
  try { code = M.callMain(args); } catch (e) { if (e && e.name === 'ExitStatus') code = e.status; else return { code: 1, stdout: out.join('\n'), stderr: String(e) }; }
  return { code: code || 0, stdout: out.join('\n') + '\n', stderr: err.join('\n') + '\n' };
}
self.runTool = runTool;

async function init() {
  postMessage({ progress: 'loading Python runtime' });
  py = await loadPyodide({ indexURL: PYODIDE });
  postMessage({ progress: 'loading numpy + scipy (first time ~30 MB)' });
  await py.loadPackage(['numpy', 'scipy', 'micropip']);
  // pyModeS is pure Python: the ADS-B decoder uses it for callsign / altitude / position fields
  try { await py.pyimport('micropip').install('pyModeS==3.6.1'); } catch (err) { postMessage({ progress: 'pyModeS unavailable: ADS-B without field decode' }); }
  postMessage({ progress: 'loading rfscan decoders' });
  await loadTools();
  const files = await (await fetch('py/manifest.json', { cache: 'no-cache' })).json();
  for (const f of files) {
    const src = await (await fetch('py/' + f, { cache: 'no-cache' })).text();
    py.FS.mkdirTree('/rf/' + f.split('/').slice(0, -1).join('/'));
    py.FS.writeFile('/rf/' + f, src);
  }
  py.FS.mkdirTree('/work');
  const browser = PURE_DECODERS.concat(Object.keys(TOOL_DECODERS).filter(d => toolWasm[TOOL_DECODERS[d]]));
  py.runPython(`
import sys, json, os, subprocess, types, itertools
sys.path.insert(0, '/rf')
from rfscan import pipeline, decoders

BROWSER = set(${JSON.stringify(browser)})
_n = itertools.count()

def _tool_run(cmd, input=None, capture_output=False, text=False, timeout=None, **kw):
    """subprocess.run() stand-in: runs the WebAssembly build of cmd[0] in this worker."""
    import js
    from pyodide.ffi import to_js
    args = list(cmd[1:])
    if input is not None:                       # stdin -> file (the tools also accept a path for '-')
        p = f"/work/stdin_{next(_n)}.raw"
        with open(p, "wb") as fh:
            fh.write(input)
        args = [p if a == "-" else a for a in args]
    files = [a.split(":", 1)[-1] for a in args if os.path.isfile(a.split(":", 1)[-1])]
    try:
        r = js.runTool(cmd[0], to_js(args), to_js(files))
    finally:
        if input is not None:
            os.remove(p)
    out, err = r.stdout, r.stderr
    if not text:
        out, err = out.encode(), err.encode()
    return subprocess.CompletedProcess(cmd, int(r.code), out, err)

_shim = types.SimpleNamespace(run=_tool_run, TimeoutExpired=subprocess.TimeoutExpired)
for _name in ("pager", "rtl433"):
    _mod = decoders.get(_name)
    if _mod is not None:
        _mod.subprocess = _shim

def _info():
    reg = decoders.registry()
    return json.dumps({"available": sorted(n for n in reg if n in BROWSER),
                       "native_only": sorted(n for n in reg if n not in BROWSER),
                       "hints": {n: getattr(m, "CAPTURE", None) for n, m in reg.items()}})

def _jsonable(o):
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
    return str(o)

def _run(path, decs, bursty):
    decs = [d for d in decs if d in BROWSER]
    r = pipeline.analyze({"path": path, "out_dir": "/work/out", "decoders": decs, "save": False,
                          "bursts": bool(bursty), "tmp_root": "/work"})
    return json.dumps(r, default=_jsonable)
`);
  return JSON.parse(py.globals.get('_info')());
}

onmessage = async e => {
  const m = e.data;
  try {
    if (!started) started = init();
    const info = await started;
    if (m.cmd === 'init') { postMessage({ id: m.id, ok: true, info }); return; }
    if (m.cmd === 'analyze') {
      const base = '/work/rec' + m.id;
      py.FS.writeFile(base + '.sigmf-meta', JSON.stringify(m.meta));
      py.FS.writeFile(base + '.sigmf-data', new Uint8Array(m.data));
      postMessage({ progress: `decoding with ${m.decoders.join(', ') || 'classifier only'}` });
      const t = performance.now();
      let out;
      try { out = py.globals.get('_run')(base, py.toPy(m.decoders), !!m.bursty); }
      finally { for (const ext of ['.sigmf-meta', '.sigmf-data']) try { py.FS.unlink(base + ext); } catch (err) { /* already gone */ } }
      postMessage({ id: m.id, ok: true, result: JSON.parse(out), ms: performance.now() - t });
    }
  } catch (err) {
    postMessage({ id: m.id, ok: false, error: String(err && err.message || err) });
  }
};
