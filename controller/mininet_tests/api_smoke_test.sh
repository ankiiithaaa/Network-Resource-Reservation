#!/usr/bin/env bash
# Quick API check with curl. Works against controller.py OR mock_controller.py.
# Usage: bash mininet_tests/api_smoke_test.sh [base_url]
BASE="${1:-http://127.0.0.1:8081}"
post() { curl -s -w "  -> HTTP %{http_code}\n" -X POST "$BASE/api/reservation" \
              -H 'Content-Type: application/json' -d "$1"; }
get()  { curl -s -w "  -> HTTP %{http_code}\n" "$BASE$1"; }

echo "### health";                 get /api/health
echo "### reserve R001 (4 Mbps)";  post '{"action":"reserve","client_id":"C1","reservation_id":"R001","bandwidth":4}'
echo "### reserve R002 (5 Mbps)";  post '{"action":"reserve","client_id":"C2","reservation_id":"R002","bandwidth":5}'
echo "### reserve R003 (4 Mbps) must be REJECTED, only 1 Mbps left"
post '{"action":"reserve","client_id":"C3","reservation_id":"R003","bandwidth":4}'
echo "### capacity";               get /api/capacity
echo "### update R001 -> 5 Mbps (fits: 10 - R002's 5, accepted)"
post '{"action":"update","reservation_id":"R001","bandwidth":5}'
echo "### update R001 -> 6 Mbps (over capacity, rejected)"
post '{"action":"update","reservation_id":"R001","bandwidth":6}'
echo "### release R001";           post '{"action":"release","reservation_id":"R001"}'
echo "### release R001 again (404 = already gone)"
post '{"action":"release","reservation_id":"R001"}'
echo "### reserve R004 with 3 s duration (C3)"
post '{"action":"reserve","client_id":"C3","reservation_id":"R004","bandwidth":2,"duration":3}'
echo "### wait 5 s for expiry"; sleep 5
echo "### R004 should now be 404"; get /api/reservations/R004
echo "### active reservations";    get /api/reservations
echo "### event log";              get /api/events
