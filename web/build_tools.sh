#!/bin/bash
# Build multimon-ng (pager: FLEX/POCSAG) and rtl_433 (ISM sensors) as WebAssembly into web/tools,
# for the browser decoders. Needs network once: Emscripten SDK + the two sources, pinned to the
# versions the native rfscan uses (multimon-ng 1.3.1, rtl_433 25.02).
# Usage: web/build_tools.sh [multimon-ng|rtl_433]   (default: both)
set -e
cd "$(dirname "$0")/.."
T=third_party
EMSDK_VERSION=4.0.10
ONLY=${1:-all}

[ -d $T/emsdk ] || git clone --depth 1 https://github.com/emscripten-core/emsdk.git $T/emsdk
(cd $T/emsdk && ./emsdk install $EMSDK_VERSION && ./emsdk activate $EMSDK_VERSION)
source "$PWD/$T/emsdk/emsdk_env.sh" >/dev/null
[ -d $T/multimon-ng ] || git clone --depth 1 --branch 1.3.1 https://github.com/EliasOenal/multimon-ng.git $T/multimon-ng
[ -d $T/rtl_433 ] || git clone --depth 1 --branch 25.02 https://github.com/merbanan/rtl_433.git $T/rtl_433

# one module instance per run, created synchronously from wasm bytes the worker already holds
LINK="-sMODULARIZE=1 -sWASM_ASYNC_COMPILATION=0 -sINVOKE_RUN=0 -sEXIT_RUNTIME=1 -sALLOW_MEMORY_GROWTH=1 -sSTACK_SIZE=8MB \
 -sFORCE_FILESYSTEM=1 -sENVIRONMENT=worker,node -sEXPORTED_RUNTIME_METHODS=callMain,FS,HEAP16,HEAPU8,stringToNewUTF8"

mkdir -p web/tools

if [ "$ONLY" = all ] || [ "$ONLY" = multimon-ng ]; then
# mm_stream.c adds streaming entry points (live pager feed) next to the unchanged main()
rm -rf $T/multimon-ng/build-wasm; mkdir -p $T/multimon-ng/build-wasm
emcc -O2 -I$T/multimon-ng -c web/tools/src/mm_stream.c -o $T/multimon-ng/build-wasm/mm_stream.o
emcmake cmake -S $T/multimon-ng -B $T/multimon-ng/build-wasm -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_DISABLE_FIND_PACKAGE_X11=ON -DCMAKE_DISABLE_FIND_PACKAGE_PulseAudio=ON \
  -DCMAKE_EXE_LINKER_FLAGS="$LINK -sEXPORT_NAME=create_multimon -sEXPORTED_FUNCTIONS=_main,_malloc,_free,_mm_stream_open,_mm_stream_feed,_mm_stream_close $PWD/$T/multimon-ng/build-wasm/mm_stream.o"
cmake --build $T/multimon-ng/build-wasm -j"$(nproc)"
cp $T/multimon-ng/build-wasm/multimon-ng.js $T/multimon-ng/build-wasm/multimon-ng.wasm web/tools/
fi

if [ "$ONLY" = all ] || [ "$ONLY" = rtl_433 ]; then
rm -rf $T/rtl_433/build-wasm
emcmake cmake -S $T/rtl_433 -B $T/rtl_433/build-wasm -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_RTLSDR=OFF -DENABLE_SOAPYSDR=OFF -DENABLE_OPENSSL=OFF -DENABLE_THREADS=OFF \
  -DBUILD_TESTING=OFF -DBUILD_DOCUMENTATION=OFF \
  -DCMAKE_EXE_LINKER_FLAGS="$LINK -sEXPORT_NAME=create_rtl433"
cmake --build $T/rtl_433/build-wasm -j"$(nproc)" --target rtl_433
cp $T/rtl_433/build-wasm/src/rtl_433.js $T/rtl_433/build-wasm/src/rtl_433.wasm web/tools/
fi

# smoke test: each factory must initialise synchronously and run main()
for t in multimon-ng:create_multimon:-h rtl_433:create_rtl433:-V; do
  f=${t%%:*}; r=${t#*:}; a=${r#*:}
  node -e "
    const fs = require('fs'), M = { wasmBinary: fs.readFileSync('web/tools/$f.wasm'), noInitialRun: true, print: () => {}, printErr: () => {} };
    require('./web/tools/$f.js')(M);
    if (typeof M.callMain !== 'function') { console.error('$f: not synchronous'); process.exit(1); }
    try { M.callMain(['$a']); } catch (e) { if (e.name !== 'ExitStatus') throw e; }
    console.log('$f: ok'); process.exit(0);"
done
ls -l web/tools
