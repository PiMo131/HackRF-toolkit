"""Generate a LoRa packet with gr-lora_sdr's TX chain (run with system python3 + prefix env).

usage: gen_tx.py out.cf32 sf bw os_factor sync_word payload_hex [n_packets]
"""
import sys
import time

import pmt
from gnuradio import blocks, gr
from gnuradio import lora_sdr

out, sf, bw, osf, sw, payload_hex = sys.argv[1:7]
npk = int(sys.argv[7]) if len(sys.argv) > 7 else 1
sf, bw, osf, sw = int(sf), int(bw), int(osf), int(sw, 0)
payload = "".join(chr(b) for b in bytes.fromhex(payload_hex))
fs = bw * osf

tb = gr.top_block()
tx = lora_sdr.lora_sdr_lora_tx(bw=bw, cr=1, has_crc=True, impl_head=False, samp_rate=fs, sf=sf,
                              ldro_mode=2, frame_zero_padd=int(2 ** sf * osf * 4), sync_word=[sw])
strobe = blocks.message_strobe(pmt.intern(payload), 400)
# packet duration upper bound
sym = 2 ** sf / bw
n = int(fs * (npk * 0.4 + 0.2))
head = blocks.head(gr.sizeof_gr_complex, n)
sink = blocks.file_sink(gr.sizeof_gr_complex, out)
tb.msg_connect(strobe, "strobe", tx, "in")
tb.connect(tx, head, sink)
tb.start()
t0 = time.time()
while head.nitems_written(0) < n and time.time() - t0 < 30:
    time.sleep(0.05)
tb.stop()
tb.wait()
