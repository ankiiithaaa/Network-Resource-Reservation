Screenshots must be taken on your own Ubuntu machine (they cannot be generated
from the code). Capture these and save them in this folder:

01_controller_started.png   Terminal 1: `ryu-manager controller.py` showing
                            "Controller ready ..." and "Reservation API listening ..."
02_switch_connected.png     Controller log showing "Switch connected: dpid=..." after starting Mininet
03_reserve_accepted.png     curl POST reserve R001 -> HTTP 200, plus the controller log "Installed meter ..."
04_flows_installed.png      `sudo ovs-ofctl -O OpenFlow13 dump-flows s1` and `dump-meters s1`
                            with the reservation active (flow with meter:1, meter rate=4000)
05_reserve_rejected.png     over-capacity reserve -> HTTP 409 insufficient_capacity, and dump-flows unchanged
06_released_flows_gone.png  after release: flow and meter no longer in dump-flows / dump-meters
07_iperf_limited.png        iperf h1 -> h4 showing ~4 Mbits/sec with the reservation active
08_e2e_all_passed.png       final "RESULT: ALL PASSED" from `sudo python3 mininet_tests/e2e_test.py`
