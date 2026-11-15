"""Ryu SDN controller for the Network Resource Reservation System (Project 15).

Run with:   ryu-manager controller.py

What it does
  * Talks OpenFlow 1.3 to Open vSwitch.
  * On switch connect: wipes stale flows/meters, installs a base flow that makes
    the switch behave like a normal learning switch (output:NORMAL).
  * Exposes the HTTP API (api_handler.py) that the TCP reservation server calls.
  * reserve  -> installs an OpenFlow meter (rate limit = reserved Mbps) and a
                higher-priority flow that sends the client's traffic through it.
  * release / expiry -> deletes that flow and meter.
  * Replays all active reservations to a switch that (re)connects.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    set_ev_cls)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

from api_handler import ReservationApi, serve_with_eventlet
from policy_store import DataPlane, ReservationManager, load_config

ETH_TYPE_IP = 0x0800
FULL_COOKIE_MASK = 0xFFFFFFFFFFFFFFFF


class OpenFlowDataPlane(DataPlane):
    """Translates reservations into OpenFlow 1.3 meter + flow messages."""

    def __init__(self, cfg, logger):
        self.dps = {}
        self.direction = cfg["enforce_direction"]
        self.priority = int(cfg["reserved_flow_priority"])
        self.logger = logger

    # -- switch bookkeeping
    def register(self, dp, active_reservations):
        self.dps[dp.id] = dp
        for res in active_reservations:  # replay after a reconnect
            self._install_on(dp, res)
        self.logger.info("Switch %016x registered (%d reservations replayed)",
                         dp.id, len(active_reservations))

    def unregister(self, dp):
        if self.dps.pop(dp.id, None):
            self.logger.info("Switch %016x disconnected", dp.id)

    def switches(self):
        return ["%016x" % dpid for dpid in sorted(self.dps)]

    # -- DataPlane interface
    def install(self, res):
        for dp in list(self.dps.values()):
            self._install_on(dp, res)
        return self.switches()

    def update(self, res):
        for dp in list(self.dps.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(self._meter_mod(dp, ofp.OFPMC_MODIFY, res))

    def remove(self, res):
        for dp in list(self.dps.values()):
            self._remove_from(dp, res)

    # -- OpenFlow helpers
    @staticmethod
    def _meter_mod(dp, command, res):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        rate_kbps = max(1, int(res.bandwidth * 1000))
        burst_kb = max(100, rate_kbps // 10)
        bands = [parser.OFPMeterBandDrop(rate=rate_kbps, burst_size=burst_kb)]
        return parser.OFPMeterMod(datapath=dp, command=command,
                                  flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST,
                                  meter_id=res.meter_id, bands=bands)

    def _matches(self, parser, ip):
        out = []
        if self.direction in ("src", "both"):
            out.append((self.priority + 1,
                        parser.OFPMatch(eth_type=ETH_TYPE_IP, ipv4_src=ip)))
        if self.direction in ("dst", "both"):
            out.append((self.priority,
                        parser.OFPMatch(eth_type=ETH_TYPE_IP, ipv4_dst=ip)))
        return out

    def _install_on(self, dp, res):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(self._meter_mod(dp, ofp.OFPMC_ADD, res))
        for prio, match in self._matches(parser, res.client_ip):
            inst = [
                parser.OFPInstructionMeter(res.meter_id),
                parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionOutput(ofp.OFPP_NORMAL)]),
            ]
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=res.meter_id, priority=prio,
                match=match, instructions=inst))
        self.logger.info("Installed meter %d + flows for %s (%s, %s Mbps) on %016x",
                         res.meter_id, res.reservation_id, res.client_ip,
                         res.bandwidth, dp.id)

    def _remove_from(self, dp, res):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            cookie=res.meter_id, cookie_mask=FULL_COOKIE_MASK,
            match=parser.OFPMatch()))
        dp.send_msg(parser.OFPMeterMod(
            datapath=dp, command=ofp.OFPMC_DELETE, flags=ofp.OFPMF_KBPS,
            meter_id=res.meter_id, bands=[]))
        self.logger.info("Removed meter %d + flows for %s on %016x",
                         res.meter_id, res.reservation_id, dp.id)


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg = load_config()
        self.dataplane = OpenFlowDataPlane(self.cfg, self.logger)
        self.manager = ReservationManager(
            self.dataplane,
            capacity_mbps=self.cfg["total_bandwidth_mbps"],
            client_map=self.cfg["client_map"],
            default_duration=self.cfg["default_duration_sec"],
            max_duration=self.cfg["max_duration_sec"])
        self.api = ReservationApi(self.manager)
        self.api_thread = hub.spawn(self._run_api)
        self.expiry_thread = hub.spawn(self._expiry_loop)
        self.logger.info("Controller ready: capacity %s Mbps, API %s:%s",
                         self.cfg["total_bandwidth_mbps"],
                         self.cfg["api_host"], self.cfg["api_port"])

    def _run_api(self):
        try:
            serve_with_eventlet(self.api, self.cfg["api_host"],
                                int(self.cfg["api_port"]))
        except Exception:
            self.logger.exception("API server stopped (is the port in use?)")

    def _expiry_loop(self):
        interval = float(self.cfg["expiry_check_interval_sec"])
        while True:
            hub.sleep(interval)
            try:
                self.manager.expire_due()
            except Exception:
                self.logger.exception("expiry check failed")

    # -- OpenFlow events
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.logger.info("Switch connected: dpid=%016x", dp.id)

        # clean slate: delete every flow and meter left from an earlier run
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=parser.OFPMatch()))
        dp.send_msg(parser.OFPMeterMod(
            datapath=dp, command=ofp.OFPMC_DELETE, flags=ofp.OFPMF_KBPS,
            meter_id=ofp.OFPM_ALL, bands=[]))

        # base flow: behave as an ordinary learning switch
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=0, match=parser.OFPMatch(),
            instructions=[parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                [parser.OFPActionOutput(ofp.OFPP_NORMAL)])]))

        self.dataplane.register(dp, self.manager.active_list())

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def _switch_gone(self, ev):
        if ev.datapath is not None:
            self.dataplane.unregister(ev.datapath)
