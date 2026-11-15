"""Reservation manager for the Network Resource Reservation System (Person 1).

Keeps track of the total link capacity and every reservation, decides whether a
request can be admitted, and expires reservations when their time is up.

Concurrency design
------------------
All shared state is protected by one lock (``self._lock``). A request is
admitted in two phases so that a slow SDN controller never blocks other
clients, yet capacity can never be over-allocated:

1. Under the lock: validate, check capacity, and store the reservation as
   PENDING. PENDING bandwidth already counts as "used".
2. Outside the lock: ask the SDN controller (``policy.install``) to install the
   network policy.
3. Under the lock again: mark ACTIVE on success, or FAILED (bandwidth returned)
   if the controller failed, so we never report a false success.

Release/expiry works the same way in reverse (RELEASING -> RELEASED/EXPIRED),
so the bandwidth is only handed out again after the controller removed the
old policy.
"""

import math
import threading
import time
from datetime import datetime

# Reservation states
PENDING = "PENDING"        # admitted, waiting for the SDN policy to be installed
ACTIVE = "ACTIVE"          # policy installed, bandwidth reserved
RELEASING = "RELEASING"    # being removed from the controller
RELEASED = "RELEASED"      # released early by the client / server shutdown
EXPIRED = "EXPIRED"        # duration elapsed
FAILED = "FAILED"          # SDN policy installation failed -> rolled back

HOLDS_BANDWIDTH = (PENDING, ACTIVE, RELEASING)


class PolicyError(Exception):
    """Raised by a policy backend when the SDN policy could not be applied."""


class NullPolicy:
    """Policy backend that does nothing (used when no controller is attached)."""

    def install(self, reservation):
        pass

    def remove(self, reservation):
        pass


def _fmt_bw(value):
    """4.0 -> '4', 2.5 -> '2.5'."""
    return ("%.3f" % value).rstrip("0").rstrip(".")


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else None


class Reservation:
    def __init__(self, reservation_id, client_id, bandwidth, duration, client_ip):
        self.reservation_id = reservation_id
        self.client_id = client_id
        self.bandwidth = bandwidth
        self.duration = duration
        self.client_ip = client_ip
        self.requested_at = time.time()
        self.start_time = None
        self.expiry_time = None
        self.end_time = None
        self.status = PENDING
        self.note = ""

    def to_dict(self):
        return {
            "reservation_id": self.reservation_id,
            "client_id": self.client_id,
            "client_ip": self.client_ip,
            "bandwidth": self.bandwidth,
            "duration": self.duration,
            "start_time": _iso(self.start_time),
            "expiry_time": _iso(self.expiry_time),
            "end_time": _iso(self.end_time),
            "remaining_seconds": (max(0, round(self.expiry_time - time.time()))
                                  if self.status == ACTIVE and self.expiry_time else None),
            "status": self.status,
            "note": self.note,
        }


class ReservationManager:
    def __init__(self, capacity_mbps=10.0, policy=None, max_duration=3600,
                 on_event=None):
        if capacity_mbps <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = float(capacity_mbps)
        self.policy = policy or NullPolicy()
        self.max_duration = max_duration
        self.on_event = on_event or (lambda message: None)
        self._lock = threading.Lock()
        self._reservations = {}          # reservation_id -> Reservation
        self._next_id = 1
        self._rejections = []            # (time, client_id, bandwidth, reason)
        self.stats = {"requests": 0, "accepted": 0, "rejected": 0,
                      "released": 0, "expired": 0, "policy_failures": 0}
        self._stop = threading.Event()
        self._expiry_thread = None

    # ------------------------------------------------------------------
    # helpers (call with the lock held)
    # ------------------------------------------------------------------
    def _used_locked(self):
        return sum(r.bandwidth for r in self._reservations.values()
                   if r.status in HOLDS_BANDWIDTH)

    def _available_locked(self):
        return max(0.0, self.capacity - self._used_locked())

    def _reject_locked(self, client_id, bandwidth, reason):
        self.stats["rejected"] += 1
        self._rejections.append((time.time(), client_id, bandwidth, reason))
        return {
            "status": "REJECTED",
            "client_id": client_id,
            "requested_bandwidth": bandwidth,
            "reason": reason,
            "remaining_bandwidth": self._available_locked(),
            "total_bandwidth": self.capacity,
        }

    @staticmethod
    def _validate(client_id, bandwidth, duration, max_duration):
        """Return (client_id, bandwidth, duration) or raise ValueError."""
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client ID must be a non-empty string")
        client_id = client_id.strip()
        if len(client_id) > 32 or not all(c.isalnum() or c in "-_" for c in client_id):
            raise ValueError("client ID may only contain letters, digits, '-' and '_' "
                             "(max 32 characters)")
        try:
            if isinstance(bandwidth, bool):
                raise TypeError
            bandwidth = float(bandwidth)
        except (TypeError, ValueError):
            raise ValueError("bandwidth must be a number (Mbps)")
        if not math.isfinite(bandwidth) or bandwidth <= 0:
            raise ValueError("bandwidth must be greater than 0 Mbps")
        try:
            if isinstance(duration, bool):
                raise TypeError
            duration = float(duration)
        except (TypeError, ValueError):
            raise ValueError("duration must be a number of seconds")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be greater than 0 seconds")
        if max_duration and duration > max_duration:
            raise ValueError("duration must not exceed %s seconds" % max_duration)
        return client_id, bandwidth, duration

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def check_capacity(self):
        """Return total / reserved / available bandwidth in Mbps."""
        with self._lock:
            used = self._used_locked()
            return {"total_bandwidth": self.capacity,
                    "reserved_bandwidth": used,
                    "available_bandwidth": max(0.0, self.capacity - used)}

    def request_reservation(self, client_id, bandwidth, duration, client_ip=None):
        """Admit or reject a request. Returns a result dict (never raises)."""
        with self._lock:
            self.stats["requests"] += 1
            try:
                client_id, bandwidth, duration = self._validate(
                    client_id, bandwidth, duration, self.max_duration)
            except ValueError as exc:
                cid = client_id if isinstance(client_id, str) else str(client_id)
                return self._reject_locked(cid, bandwidth, "Invalid request: %s" % exc)

            for r in self._reservations.values():
                if r.client_id == client_id and r.status in HOLDS_BANDWIDTH:
                    return self._reject_locked(
                        client_id, bandwidth,
                        "Client %s already holds reservation %s"
                        % (client_id, r.reservation_id))

            if bandwidth > self.capacity:
                return self._reject_locked(
                    client_id, bandwidth,
                    "Requested %s Mbps exceeds total capacity of %s Mbps"
                    % (_fmt_bw(bandwidth), _fmt_bw(self.capacity)))

            available = self._available_locked()
            if bandwidth > available + 1e-9:
                return self._reject_locked(
                    client_id, bandwidth,
                    "Insufficient bandwidth: requested %s Mbps, only %s Mbps available"
                    % (_fmt_bw(bandwidth), _fmt_bw(available)))

            rid = "R%03d" % self._next_id
            self._next_id += 1
            res = Reservation(rid, client_id, bandwidth, duration, client_ip)
            self._reservations[rid] = res          # PENDING: already counts as used

        # Phase 2: talk to the SDN controller without holding the lock.
        try:
            self.policy.install(res)
        except Exception as exc:                   # PolicyError, network errors, ...
            with self._lock:
                res.status = FAILED
                res.end_time = time.time()
                res.note = "SDN policy installation failed: %s" % exc
                self.stats["policy_failures"] += 1
                result = self._reject_locked(
                    client_id, bandwidth,
                    "SDN policy installation failed, reservation rolled back (%s)" % exc)
            self.on_event("Reservation %s for %s ROLLED BACK: %s" % (rid, client_id, exc))
            return result

        with self._lock:
            now = time.time()
            res.status = ACTIVE
            res.start_time = now
            res.expiry_time = now + duration
            self.stats["accepted"] += 1
            result = {
                "status": "ACCEPTED",
                "reservation_id": rid,
                "client_id": client_id,
                "allocated_bandwidth": bandwidth,
                "duration": duration,
                "expiry_time": _iso(res.expiry_time),
                "remaining_bandwidth": self._available_locked(),
                "total_bandwidth": self.capacity,
            }
        return result

    def release_reservation(self, reservation_id, client_id=None, reason=RELEASED):
        """Release an ACTIVE reservation (reason RELEASED or EXPIRED).

        If client_id is given, only that client's reservation can be released.
        """
        with self._lock:
            res = self._reservations.get(reservation_id)
            if res is None:
                return {"status": "ERROR", "reason": "Unknown reservation %s" % reservation_id}
            if client_id is not None and res.client_id != client_id:
                return {"status": "ERROR",
                        "reason": "Reservation %s does not belong to %s"
                                  % (reservation_id, client_id)}
            if res.status != ACTIVE:
                return {"status": "ERROR",
                        "reason": "Reservation %s is %s, not ACTIVE"
                                  % (reservation_id, res.status)}
            res.status = RELEASING                # still holds bandwidth

        try:
            self.policy.remove(res)
            note = ""
        except Exception as exc:
            # The controller has its own copy; we still free our bandwidth so
            # the server does not leak capacity, but record the problem.
            note = "SDN policy removal failed: %s" % exc
            self.on_event("WARNING: %s policy removal failed: %s" % (reservation_id, exc))

        with self._lock:
            res.status = reason
            res.end_time = time.time()
            res.note = note
            self.stats["expired" if reason == EXPIRED else "released"] += 1
            available = self._available_locked()
        verb = "expired" if reason == EXPIRED else "released"
        self.on_event("Reservation %s %s. %s Mbps released. Available bandwidth: %s Mbps."
                      % (reservation_id, verb, _fmt_bw(res.bandwidth), _fmt_bw(available)))
        return {"status": "OK", "reservation_id": reservation_id,
                "released_bandwidth": res.bandwidth,
                "remaining_bandwidth": available, "total_bandwidth": self.capacity}

    def expire_reservations(self):
        """Expire every ACTIVE reservation whose time is up. Returns their IDs."""
        now = time.time()
        with self._lock:
            due = [r.reservation_id for r in self._reservations.values()
                   if r.status == ACTIVE and r.expiry_time <= now]
        for rid in due:
            self.release_reservation(rid, reason=EXPIRED)
        return due

    def release_all(self):
        """Release every ACTIVE reservation (used on server shutdown)."""
        with self._lock:
            active = [r.reservation_id for r in self._reservations.values()
                      if r.status == ACTIVE]
        for rid in active:
            self.release_reservation(rid)
        return active

    def get_utilization(self):
        with self._lock:
            used = self._used_locked()
            active = sum(1 for r in self._reservations.values() if r.status == ACTIVE)
            stats = dict(self.stats)
        decided = stats["accepted"] + stats["rejected"]
        return {
            "total_bandwidth": self.capacity,
            "reserved_bandwidth": used,
            "available_bandwidth": max(0.0, self.capacity - used),
            "utilization_percent": round(100.0 * used / self.capacity, 1),
            "active_reservations": active,
            "acceptance_rate_percent": round(100.0 * stats["accepted"] / decided, 1)
                                       if decided else None,
            **stats,
        }

    def get_reservations(self, active_only=False):
        with self._lock:
            items = [r.to_dict() for r in self._reservations.values()
                     if not active_only or r.status in HOLDS_BANDWIDTH]
        return items

    def get_rejections(self):
        with self._lock:
            return [{"time": _iso(t), "client_id": c, "bandwidth": b, "reason": why}
                    for t, c, b, why in self._rejections]

    # ------------------------------------------------------------------
    # background expiry
    # ------------------------------------------------------------------
    def start_expiry_thread(self, interval=1.0):
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.expire_reservations()
                except Exception as exc:          # never let the thread die
                    self.on_event("ERROR in expiry thread: %s" % exc)

        self._expiry_thread = threading.Thread(target=loop, name="expiry", daemon=True)
        self._expiry_thread.start()

    def stop(self):
        self._stop.set()
        if self._expiry_thread:
            self._expiry_thread.join(timeout=2)
