#!/bin/bash
# rfscan setup: system packages, userspace libhackrf (HackRF Pro support), gr-lora_sdr, Python venv.
# Re-runnable; skips steps that are already done.
set -e
D="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$D/third_party/prefix"
mkdir -p "$D/third_party"

echo "== 1. system packages (needs sudo)"
PKGS="cmake build-essential git pkg-config libusb-1.0-0-dev libfftw3-dev rtl-433 multimon-ng gnuradio gnuradio-dev
      gr-gsm gr-osmosdr soapysdr-module-hackrf python3-soapysdr inspectrum readsb welle.io libvolk-dev pybind11-dev"
missing=$(for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || echo "$p"; done)
if [ -n "$missing" ]; then sudo apt-get install -y $missing; else echo "   all present"; fi

echo "== 2. libhackrf + hackrf tools from git (HackRF Pro needs >= 2026.01) -> $PREFIX"
if [ ! -e "$PREFIX/lib/libhackrf.so.0" ]; then
  [ -d "$D/third_party/hackrf" ] || git clone --depth 1 https://github.com/greatscottgadgets/hackrf.git "$D/third_party/hackrf"
  cmake -S "$D/third_party/hackrf/host" -B "$D/third_party/hackrf/host/build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release -DINSTALL_UDEV_RULES=OFF
  make -C "$D/third_party/hackrf/host/build" -j"$(nproc)" install
fi
LD_LIBRARY_PATH="$PREFIX/lib" "$PREFIX/bin/hackrf_info" | grep -E "Board ID|Firmware" || true
echo "   (HackRF Pro firmware must be >= 2026.01.3; older builds ignore the sample rate."
echo "    Update: LD_LIBRARY_PATH=$PREFIX/lib $PREFIX/bin/hackrf_spiflash -w hackrf_pro_usb.bin -R"
echo "    using firmware-bin/ from https://github.com/greatscottgadgets/hackrf/releases)"

echo "== 3. gr-lora_sdr (LoRa decode to CRC) -> $PREFIX"
if [ ! -d "$PREFIX/lib/python3"*"/site-packages/gnuradio/lora_sdr" ] 2>/dev/null && \
   ! ls "$PREFIX"/lib/python3*/site-packages/gnuradio/lora_sdr >/dev/null 2>&1; then
  [ -d "$D/third_party/gr-lora_sdr" ] || git clone --depth 1 https://github.com/tapparelj/gr-lora_sdr.git "$D/third_party/gr-lora_sdr"
  cmake -S "$D/third_party/gr-lora_sdr" -B "$D/third_party/gr-lora_sdr/build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release
  make -C "$D/third_party/gr-lora_sdr/build" -j"$(nproc)" install
fi

echo "== 4. Python venv"
if [ ! -x "$D/.venv/bin/python" ]; then
  if command -v uv >/dev/null; then uv venv --system-site-packages "$D/.venv"; else python3 -m venv --system-site-packages "$D/.venv"; fi
fi
if command -v uv >/dev/null; then uv pip install --python "$D/.venv/bin/python" -q numpy scipy pyModeS sigmf
else "$D/.venv/bin/pip" install -q numpy scipy pyModeS sigmf; fi

echo "== done. Try:  $D/rfscan.sh decoders   and   $D/rfscan.sh scan --duration 900"
