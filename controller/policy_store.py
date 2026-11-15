"""Reservation bookkeeping and admission logic for the SDN controller.

This module is pure Python (standard library only). It never speaks OpenFlow
itself: it decides what is allowed and tells a DataPlane object what to do.
That keeps it unit-testable on any OS, and lets controller.py plug in the real
OpenFlow data plane while mock_controller.py plugs in a log-only one.
"""
import collections
import ipaddress
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("reservation")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "api_host": "127.0.0.1",
    "api_port": 8081,
    "openflow_port": 6653,
    "total_bandwidth_mbps": 10,
    "default_duration_sec": 0,       # 0 = reservation never expires on its own
    "max_duration_sec": 3600,        # 0 = no upper limit
    "expiry_check_interval_sec": 1,
    "enforce_direction": "src",      # src | dst | both
    "reserved_flow_priority": 100,
    "client_map": {},
}


def load_config(path=None):
    """Load config/config.json (or $SDN_CONFIG, or an explicit path)."""
    cfg = dict(DEFAULT_CONFIG)
    path = path or os.environ.get("SDN_CONFIG") or os.path.join(
        BASE_DIR, "config", "config.json")
    try:
        with open(path) as fh:
            cfg.update(json.load(fh))
        log.info("Loaded config from %s", path)
    except FileNotFoundError:
        log.warning("Config %s not found, using defaults", path)
    return cfg


class ReservationError(Exception):
    """Raised for any request the controller refuses. Carries an HTTP status."""

    def __init__(self, http_status, code, message, **extra):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self):
        body = {"status": "error", "error": self.code, "message": self.message}
        body.update(self.extra)
        return body


class DataPlane:
    """What the manager needs from the network side."""

    def install(self, res):
        """Install policy for a new reservation. Return list of switch ids."""
        raise NotImplementedError

    def update(self, res):
        """Change the rate of an existing reservation."""
        raise NotImplementedError

    def remove(self, res):
        """Remove all policy belonging to a reservation."""
        raise NotImplementedError

    def switches(self):
        return []


class LogOnlyDataPlane(DataPlane):
    """No network: just records calls. Used by tests and mock_controller.py."""

    def __init__(self, switches=("dry-run-s1",)):
        self._switches = list(switches)
        self.calls = []

    def switches(self):
        return list(self._switches)

    def install(self, res):
        self.calls.append(("install", res.reservation_id, res.bandwidth))
        log.info("[dry-run] install %s client=%s ip=%s %s Mbps meter=%s",
                 res.reservation_id, res.client_id, res.client_ip,
                 res.bandwidth, res.meter_id)
        return self.switches()

    def update(self, res):
        self.calls.append(("update", res.reservation_id, res.bandwidth))
        log.info("[dry-run] update %s -> %s Mbps", res.reservation_id,
                 res.bandwidth)

    def remove(self, res):
        self.calls.append(("remove", res.reservation_id, res.bandwidth))
        log.info("[dry-run] remove %s", res.reservation_id)


class Reservation:
    __slots__ = ("reservation_id", "client_id", "client_ip", "bandwidth",
                 "meter_id", "created_at", "expires_at")

    def __init__(self, reservation_id, client_id, client_ip, bandwidth,
                 meter_id, created_at, expires_at):
        self.reservation_id = reservation_id
        self.client_id = client_id
        self.client_ip = client_ip
        self.bandwidth = bandwidth
        self.meter_id = meter_id
        self.created_at = created_at
        self.expires_at = expires_at

    def to_dict(self, now=None):
        now = time.time() if now is None else now
        out = {
            "reservation_id": self.reservation_id,
            "client_id": self.client_id,
            "client_ip": self.client_ip,
            "bandwidth_mbps": self.bandwidth,
            "meter_id": self.meter_id,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
        }
        if self.expires_at:
            out["remaining_sec"] = max(0, round(self.expires_at - now, 1))
        return out


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class ReservationManager:
    def __init__(self, dataplane, capacity_mbps=10, client_map=None,
                 default_duration=0, max_duration=0, clock=time.time):
        self.dataplane = dataplane
        self.capacity = float(capacity_mbps)
        self.client_map = dict(client_map or {})
        self.default_duration = default_duration or 0
        self.max_duration = max_duration or 0
        self.clock = clock
        self._lock = threading.RLock()
        self._active = {}
        self._events = collections.deque(maxlen=500)
        self.stats = {"accepted": 0, "rejected": 0, "released": 0,
                      "expired": 0, "updated": 0}

    # ---- validation helpers -------------------------------------------
    @staticmethod
    def _text(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ReservationError(400, "bad_request",
                                   "'%s' must be a non-empty string" % field)
        return value.strip()

    @staticmethod
    def _bandwidth(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ReservationError(400, "bad_request",
                                   "'bandwidth' must be a positive number (Mbps)")
        return value

    def _duration(self, value):
        if value is None:
            return self.default_duration
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value < 0:
            raise ReservationError(400, "bad_request",
                                   "'duration' must be a number of seconds >= 0")
        if self.max_duration and value > self.max_duration:
            raise ReservationError(
                400, "bad_request",
                "'duration' exceeds max_duration_sec (%s)" % self.max_duration)
        return value or self.default_duration

    def _resolve_ip(self, client_id, client_ip):
        ip = client_ip or self.client_map.get(client_id)
        if not ip:
            raise ReservationError(
                400, "unknown_client",
                "No IP known for client '%s'. Add it to client_map in "
                "config/config.json or send 'client_ip'." % client_id)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise ReservationError(400, "bad_request", "invalid client_ip '%s'" % ip)
        if addr.version != 4:
            raise ReservationError(400, "bad_request", "only IPv4 is supported")
        return str(addr)

    def _event(self, kind, **fields):
        entry = {"time": _iso(self.clock()), "event": kind}
        entry.update(fields)
        self._events.append(entry)
        log.info("%s %s", kind, fields)

    def _reject(self, rid, cid, http_status, code, message, **extra):
        self.stats["rejected"] += 1
        self._event("rejected", reservation_id=rid, client_id=cid,
                    reason=code)
        return ReservationError(http_status, code, message, **extra)

    def _used_locked(self):
        return sum(r.bandwidth for r in self._active.values())

    def _alloc_meter_locked(self):
        used = {r.meter_id for r in self._active.values()}
        mid = 1
        while mid in used:
            mid += 1
        return mid

    # ---- operations ---------------------------------------------------
    def reserve(self, reservation_id, client_id, bandwidth, duration=None,
                client_ip=None):
        rid = self._text(reservation_id, "reservation_id")
        cid = self._text(client_id, "client_id")
        bw = self._bandwidth(bandwidth)
        dur = self._duration(duration)
        ip = self._resolve_ip(cid, client_ip)
        with self._lock:
            now = self.clock()
            existing = self._active.get(rid)
            if existing:
                if existing.client_id == cid and existing.bandwidth == bw:
                    out = existing.to_dict(now)
                    out["idempotent"] = True
                    return out
                raise self._reject(rid, cid, 409, "duplicate_reservation",
                                   "reservation_id '%s' already exists" % rid)
            for other in self._active.values():
                if other.client_id == cid or other.client_ip == ip:
                    raise self._reject(
                        rid, cid, 409, "client_already_reserved",
                        "client '%s' already holds reservation '%s'; release "
                        "or update it instead" % (cid, other.reservation_id))
            used = self._used_locked()
            available = self.capacity - used
            if bw > available + 1e-9:
                raise self._reject(
                    rid, cid, 409, "insufficient_capacity",
                    "requested %s Mbps but only %s Mbps available"
                    % (bw, round(available, 3)),
                    requested_mbps=bw, available_mbps=round(available, 3))
            res = Reservation(rid, cid, ip, bw, self._alloc_meter_locked(),
                              now, now + dur if dur else None)
            try:
                installed_on = self.dataplane.install(res)
            except Exception as exc:  # data plane failure -> nothing allocated
                log.exception("install failed")
                raise ReservationError(500, "dataplane_error", str(exc))
            self._active[rid] = res
            self.stats["accepted"] += 1
            self._event("reserved", reservation_id=rid, client_id=cid,
                        bandwidth_mbps=bw, meter_id=res.meter_id,
                        switches=installed_on)
            out = res.to_dict(now)
            out["installed_on_switches"] = installed_on
            return out

    def update(self, reservation_id, bandwidth, duration=None):
        rid = self._text(reservation_id, "reservation_id")
        bw = self._bandwidth(bandwidth)
        with self._lock:
            res = self._active.get(rid)
            if not res:
                raise ReservationError(404, "not_found",
                                       "no active reservation '%s'" % rid)
            available = self.capacity - (self._used_locked() - res.bandwidth)
            if bw > available + 1e-9:
                raise self._reject(
                    rid, res.client_id, 409, "insufficient_capacity",
                    "requested %s Mbps but only %s Mbps available"
                    % (bw, round(available, 3)),
                    requested_mbps=bw, available_mbps=round(available, 3))
            old_bw, old_exp = res.bandwidth, res.expires_at
            res.bandwidth = bw
            if duration is not None:
                dur = self._duration(duration)
                res.expires_at = self.clock() + dur if dur else None
            try:
                self.dataplane.update(res)
            except Exception as exc:
                res.bandwidth, res.expires_at = old_bw, old_exp
                log.exception("update failed")
                raise ReservationError(500, "dataplane_error", str(exc))
            self.stats["updated"] += 1
            self._event("updated", reservation_id=rid, old_mbps=old_bw,
                        new_mbps=bw)
            return res.to_dict(self.clock())

    def release(self, reservation_id):
        rid = self._text(reservation_id, "reservation_id")
        with self._lock:
            res = self._active.get(rid)
            if not res:
                raise ReservationError(404, "not_found",
                                       "no active reservation '%s' (already "
                                       "released or expired?)" % rid)
            try:
                self.dataplane.remove(res)
            except Exception as exc:  # keep it active so a retry is possible
                log.exception("remove failed")
                raise ReservationError(500, "dataplane_error", str(exc))
            del self._active[rid]
            self.stats["released"] += 1
            self._event("released", reservation_id=rid,
                        bandwidth_mbps=res.bandwidth)
            return {"reservation_id": rid, "released_mbps": res.bandwidth}

    def note_rejected(self, reservation_id, client_id, reason=""):
        """Record a request the TCP server rejected. Allocates nothing."""
        rid = self._text(reservation_id, "reservation_id")
        cid = self._text(client_id, "client_id")
        with self._lock:
            if rid in self._active:
                raise ReservationError(
                    409, "duplicate_reservation",
                    "'%s' is an active reservation, use release" % rid)
            self.stats["rejected"] += 1
            self._event("rejected_by_server", reservation_id=rid,
                        client_id=cid, reason=reason or "unspecified")
            return {"reservation_id": rid, "allocated": False}

    def expire_due(self):
        """Remove every reservation whose time is up. Returns what was removed."""
        removed = []
        with self._lock:
            now = self.clock()
            due = [r for r in self._active.values()
                   if r.expires_at and r.expires_at <= now]
            for res in due:
                error = None
                try:
                    self.dataplane.remove(res)
                except Exception as exc:
                    error = str(exc)
                    log.exception("remove during expiry failed")
                del self._active[res.reservation_id]
                self.stats["expired"] += 1
                self._event("expired", reservation_id=res.reservation_id,
                            bandwidth_mbps=res.bandwidth, error=error)
                removed.append(res.reservation_id)
        return removed

    # ---- queries ------------------------------------------------------
    def get(self, reservation_id):
        with self._lock:
            res = self._active.get(reservation_id)
            if not res:
                raise ReservationError(404, "not_found",
                                       "no active reservation '%s'" % reservation_id)
            return res.to_dict(self.clock())

    def active_list(self):
        """Reservation objects (used to replay policy to a new switch)."""
        with self._lock:
            return list(self._active.values())

    def list(self):
        with self._lock:
            now = self.clock()
            return [r.to_dict(now) for r in self._active.values()]

    def events(self):
        with self._lock:
            return list(self._events)

    def status(self):
        with self._lock:
            used = self._used_locked()
            return {
                "capacity_mbps": self.capacity,
                "used_mbps": used,
                "available_mbps": round(self.capacity - used, 3),
                "active_count": len(self._active),
                "stats": dict(self.stats),
                "switches": self.dataplane.switches(),
            }
