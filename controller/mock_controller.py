#!/usr/bin/env python3
"""Run the reservation API WITHOUT Ryu, Mininet or Open vSwitch.

Works on any OS (Windows/macOS/Linux) with plain Python 3.7+.
Same endpoints, same JSON, same admission and expiry logic as controller.py,
but "installing a flow" just prints a log line.

Use it to develop/test the TCP server integration before the real controller
is available:   python3 mock_controller.py
"""
import argparse
import logging
import threading
import time

from api_handler import ReservationApi, make_standalone_server
from policy_store import LogOnlyDataPlane, ReservationManager, load_config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    manager = ReservationManager(
        LogOnlyDataPlane(), cfg["total_bandwidth_mbps"], cfg["client_map"],
        cfg["default_duration_sec"], cfg["max_duration_sec"])

    def expiry_loop():
        while True:
            time.sleep(float(cfg["expiry_check_interval_sec"]))
            manager.expire_due()

    threading.Thread(target=expiry_loop, daemon=True).start()

    host = args.host or cfg["api_host"]
    port = args.port or int(cfg["api_port"])
    server = make_standalone_server(ReservationApi(manager), host, port)
    logging.info("MOCK controller API on http://%s:%s (no real switches)",
                 host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
