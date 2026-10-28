#!/bin/sh
# Copy the rfscan analysis code into web/py so the browser (Pyodide) can run the same decoders,
# and regenerate the band table. Run after changing anything in rfscan/.
set -e
cd "$(dirname "$0")/.."
rm -rf web/py && mkdir -p web/py/rfscan/decoders
cp rfscan/__init__.py rfscan/bandplan.py rfscan/dsp.py rfscan/recording.py rfscan/pipeline.py web/py/rfscan/
cp rfscan/decoders/*.py web/py/rfscan/decoders/
(cd web/py && find rfscan -name '*.py' | sort | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin]))') > web/py/manifest.json
.venv/bin/python - > web/bands.js <<'PY'
import json, sys
sys.path.insert(0, '.')
from rfscan.bandplan import BANDS
b = sorted(BANDS, key=lambda b: b.hi - b.lo)
print('// Generated from rfscan/bandplan.py by web/build.sh (narrowest first): [lo_MHz, hi_MHz, service, category, decoders]')
print('const BANDS = ' + json.dumps([[x.lo, x.hi, x.service, x.category, list(x.decoders)] for x in b], ensure_ascii=False).replace('], [', '],\n  [') + ';')
PY
echo "web/py: $(wc -l < web/py/manifest.json) manifest, $(ls web/py/rfscan/decoders | wc -l) decoder files"
