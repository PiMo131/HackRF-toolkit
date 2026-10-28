import sys, runpy
from gnuradio import pdu, network
pdu.socket_pdu = network.socket_pdu
sys.argv = ["grgsm_decode"] + sys.argv[1:]
runpy.run_path("/usr/bin/grgsm_decode", run_name="__main__")
