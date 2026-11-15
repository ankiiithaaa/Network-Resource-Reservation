"""HTTP/JSON API between Person 1's TCP reservation server and the controller.

Implemented as a plain WSGI app so the same code runs:
  * inside Ryu (served by eventlet, on Ryu's own green-thread hub, which makes
    it safe to send OpenFlow messages from request handlers), and
  * standalone with wsgiref (mock_controller.py and the unit tests).

Endpoints (all JSON):
  POST   /api/reservation          body {"action": "reserve"|"release"|"update"|"reject", ...}
  GET    /api/health
  GET    /api/capacity
  GET    /api/reservations
  GET    /api/reservations/<reservation_id>
  DELETE /api/reservations/<reservation_id>      (same as action=release)
  GET    /api/events
"""
import json
import logging
from http import HTTPStatus
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from policy_store import ReservationError

log = logging.getLogger("api")

MAX_BODY = 64 * 1024


class ReservationApi:
    def __init__(self, manager):
        self.manager = manager

    # ---- WSGI entry point --------------------------------------------
    def __call__(self, environ, start_response):
        try:
            status, body = self._route(environ)
        except ReservationError as err:
            status, body = err.http_status, err.to_dict()
        except Exception as exc:  # never let a bug kill the server
            log.exception("unhandled error")
            status, body = 500, {"status": "error", "error": "internal_error",
                                 "message": str(exc)}
        data = json.dumps(body, indent=2).encode()
        start_response("%d %s" % (status, HTTPStatus(status).phrase), [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(data))),
        ])
        return [data]

    # ---- routing -----------------------------------------------------
    def _route(self, environ):
        method = environ["REQUEST_METHOD"].upper()
        path = environ.get("PATH_INFO", "").rstrip("/") or "/"
        m = self.manager

        if method == "GET" and path == "/api/health":
            return 200, {"status": "ok", "switches": m.dataplane.switches()}
        if method == "GET" and path == "/api/capacity":
            return 200, dict(m.status(), status="ok")
        if method == "GET" and path == "/api/reservations":
            return 200, {"status": "ok", "reservations": m.list(),
                         "summary": m.status()}
        if method == "GET" and path == "/api/events":
            return 200, {"status": "ok", "events": m.events()}
        if path.startswith("/api/reservations/"):
            rid = path[len("/api/reservations/"):]
            if method == "GET":
                return 200, {"status": "ok", "reservation": m.get(rid)}
            if method == "DELETE":
                return 200, dict(m.release(rid), status="ok")
        if method == "POST" and path in ("/api/reservation", "/api/reservations"):
            return self._action(self._json_body(environ))

        if path.startswith("/api/"):
            return 405 if method not in ("GET", "POST", "DELETE") else 404, {
                "status": "error", "error": "not_found",
                "message": "%s %s is not an endpoint" % (method, path)}
        return 404, {"status": "error", "error": "not_found",
                     "message": "unknown path"}

    def _action(self, body):
        action = body.get("action")
        m = self.manager
        if action == "reserve":
            for field in ("reservation_id", "client_id", "bandwidth"):
                if field not in body:
                    raise ReservationError(400, "bad_request",
                                           "missing field '%s'" % field)
            res = m.reserve(body["reservation_id"], body["client_id"],
                            body["bandwidth"], body.get("duration"),
                            body.get("client_ip"))
            return 200, {"status": "ok", "action": "reserve", "reservation": res}
        if action == "release":
            if "reservation_id" not in body:
                raise ReservationError(400, "bad_request",
                                       "missing field 'reservation_id'")
            return 200, dict(m.release(body["reservation_id"]),
                             status="ok", action="release")
        if action == "update":
            for field in ("reservation_id", "bandwidth"):
                if field not in body:
                    raise ReservationError(400, "bad_request",
                                           "missing field '%s'" % field)
            res = m.update(body["reservation_id"], body["bandwidth"],
                           body.get("duration"))
            return 200, {"status": "ok", "action": "update", "reservation": res}
        if action == "reject":
            for field in ("reservation_id", "client_id"):
                if field not in body:
                    raise ReservationError(400, "bad_request",
                                           "missing field '%s'" % field)
            return 200, dict(m.note_rejected(body["reservation_id"],
                                             body["client_id"],
                                             body.get("reason", "")),
                             status="ok", action="reject")
        raise ReservationError(
            400, "bad_request",
            "'action' must be one of: reserve, release, update, reject")

    @staticmethod
    def _json_body(environ):
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            raise ReservationError(400, "bad_request",
                                   "JSON body required (max %d bytes)" % MAX_BODY)
        raw = environ["wsgi.input"].read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            raise ReservationError(400, "bad_request", "body is not valid JSON")
        if not isinstance(body, dict):
            raise ReservationError(400, "bad_request",
                                   "JSON body must be an object")
        return body


# ---- servers ----------------------------------------------------------
def serve_with_eventlet(app, host, port):
    """Used by controller.py: runs on Ryu's eventlet hub (blocking call)."""
    import eventlet
    from eventlet import wsgi

    sock = eventlet.listen((host, port))
    log.info("Reservation API listening on http://%s:%s", host, port)
    wsgi.server(sock, app, log_output=False)


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)


def make_standalone_server(app, host, port):
    """Used by mock_controller.py and tests (port 0 = pick a free port)."""
    return make_server(host, port, app, server_class=_ThreadingServer,
                       handler_class=_QuietHandler)
