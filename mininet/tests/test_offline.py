import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from measurements import parse_iperf, parse_ping
from plot_results import compare_tcp
from qos import class_bytes, policy_commands
from test_cases import METRICS, create_records
from topology import controller_connected, host_ip, load_config, verify_switch


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_default_addresses_ports_and_capacity(self):
        self.assertEqual([host_ip(self.cfg, h) for h in ("h1", "h2", "h3")],
                         ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(self.cfg["benchmark"]["capacity_mbps"], 10)
        self.assertEqual(self.cfg["benchmark"]["reserved_mbps"], 6)

    def test_bad_config(self):
        self.cfg["hosts"]["h2"]["ip"] = self.cfg["hosts"]["h1"]["ip"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text(json.dumps(self.cfg))
            with self.assertRaisesRegex(ValueError, "distinct"):
                load_config(path)

    def test_server_host_h4(self):
        server = self.cfg["server_host"]
        self.assertEqual((server["name"], server["ip"], server["port"]),
                         ("h4", "10.0.0.4/24", 4))
        self.assertTrue(server["root_namespace"])
        self.cfg["server_host"]["ip"] = self.cfg["hosts"]["h3"]["ip"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text(json.dumps(self.cfg))
            with self.assertRaisesRegex(ValueError, "server_host"):
                load_config(path)

    def test_controller_target_cannot_be_confused_with_other_controller(self):
        rows = {"headings": ["target", "is_connected"], "data": [
            ["tcp:127.0.0.1:6653", False], ["tcp:127.0.0.2:6653", True]]}
        with patch("topology.run_command", return_value=json.dumps(rows)):
            self.assertFalse(controller_connected(self.cfg))
        rows["data"][0][1] = True
        with patch("topology.run_command", return_value=json.dumps(rows)):
            self.assertTrue(controller_connected(self.cfg))

    def test_verify_ports_rejects_wrong_port_mapping(self):
        with patch("topology.run_command", side_effect=["1", "3"]):
            with self.assertRaisesRegex(RuntimeError, "Expected s1-eth2"):
                verify_switch(self.cfg, "standalone")

    def test_policy_tree_and_filter_are_specific(self):
        static = [" ".join(x) for x in policy_commands(self.cfg, "static")]
        reserve = [" ".join(x) for x in policy_commands(self.cfg, "reservation")]
        self.assertEqual(static[:2], reserve[:2])
        self.assertFalse(any("filter add" in cmd for cmd in static))
        self.assertTrue(any("classid 1:10 htb rate 6mbit ceil 10mbit" in cmd for cmd in reserve))
        self.assertTrue(any("classid 1:20 htb rate 4mbit ceil 10mbit" in cmd for cmd in reserve))
        filters = [cmd for cmd in reserve if "filter add" in cmd]
        self.assertEqual(len(filters), 2)
        for protocol in ("tcp", "udp"):
            self.assertTrue(any(f"src_ip 10.0.0.1 dst_ip 10.0.0.3 ip_proto {protocol} "
                                "dst_port 5201 classid 1:10" in f for f in filters))

    def test_class_byte_counter(self):
        dump = ("class htb 1:1 root rate 10Mbit\n Sent 555 bytes 3 pkt\n"
                "class htb 1:10 parent 1:1 rate 6Mbit\n Sent 123 bytes 1 pkt\n"
                "class htb 1:20 parent 1:1 rate 4Mbit\n Sent 432 bytes 2 pkt\n")
        self.assertEqual(class_bytes(dump, "1:10"), 123)
        self.assertEqual(class_bytes(dump, "1:20"), 432)
        with self.assertRaises(ValueError):
            class_bytes(dump, "1:15")

    def test_ping_success(self):
        data = parse_ping("5 packets transmitted, 5 received, 0% packet loss\n"
                          "rtt min/avg/max/mdev = 0.100/0.200/0.300/0.045 ms")
        self.assertEqual(data["rtt_avg_ms"], 0.2)
        self.assertEqual(data["icmp_loss_pct"], 0)

    def test_ping_no_reply_has_no_rtt(self):
        data = parse_ping("5 packets transmitted, 0 received, +5 errors, 100% packet loss")
        self.assertIsNone(data["rtt_avg_ms"])
        self.assertEqual(data["icmp_loss_pct"], 100)

    def test_tcp_uses_receiver_summary(self):
        data = parse_iperf({"end": {"sum_received": {"bits_per_second": 6100000,
                                                     "sender": False},
                                    "sum_sent": {"bits_per_second": 6200000}}}, "TCP")
        self.assertEqual(data, {"throughput_mbps": 6.1})

    def test_legacy_and_modern_udp_uses_server_data(self):
        s = {"bits_per_second": 3900000, "packets": 500, "lost_percent": 20, "jitter_ms": 0.3}
        modern = {"end": {"sum_received": s}}
        legacy = {"end": {"sum": {**s, "bits_per_second": 12000000}},
                  "intervals": [{"sum": {"seconds": 1, "bytes": 487500, "sender": False}}]}
        for sample in (modern, legacy):
            with self.subTest(sample=sample):
                self.assertEqual(parse_iperf(sample, "UDP"),
                                 {"throughput_mbps": 3.9, "udp_loss_pct": 20,
                                  "udp_jitter_ms": 0.3})

    def test_failed_iperf_is_never_a_zero_measurement(self):
        with self.assertRaisesRegex(ValueError, "unable to connect"):
            parse_iperf({"error": "unable to connect"}, "TCP")
        with self.assertRaisesRegex(ValueError, "marked as sender"):
            parse_iperf({"end": {"sum_received": {"bits_per_second": 12, "sender": True}}}, "TCP")

    def test_csv_blanks_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            rec = create_records(out, out / "checks.csv")
            rec.metric(policy="static", protocol="ICMP", icmp_loss_pct=0, rtt_avg_ms=None)
            with (out / "performance_results.csv").open(newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual((row["icmp_loss_pct"], row["rtt_avg_ms"]), ("0", ""))
            with self.assertRaises(ValueError):
                create_records(out, out / "checks.csv")

    def test_comparison_requires_matched_repeats(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, METRICS)
                writer.writeheader()
                for policy, rates in (("static", (5, 5)), ("reservation", (6, 4))):
                    for source, rate in zip(("h1", "h2"), rates):
                        writer.writerow(dict(scenario="dual_tcp", switch_mode="controller",
                                             policy=policy, source=source, repeat=1,
                                             throughput_mbps=rate))
            self.assertEqual(compare_tcp(path)["mean_mbps"]["reservation"]["combined"], 10)
            with path.open("a", newline="") as f:
                csv.DictWriter(f, METRICS).writerow(dict(scenario="dual_tcp",
                    switch_mode="controller", policy="static", source="h1", repeat=2,
                    throughput_mbps=7))
            with self.assertRaisesRegex(ValueError, "matched"):
                compare_tcp(path)

    def test_cli_help_does_not_require_mininet(self):
        root = Path(__file__).resolve().parents[1]
        for file in ("topology.py", "test_cases.py", "plot_results.py"):
            with self.subTest(file=file):
                p = subprocess.run([sys.executable, str(root / file), "--help"],
                                   capture_output=True, text=True, timeout=10)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("usage:", p.stdout)


if __name__ == "__main__":
    unittest.main()
