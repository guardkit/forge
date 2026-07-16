#!/usr/bin/env python3
"""F4 FEATURE-BEHAVIOUR gate TEMPLATE — parameterized by endpoint + assertions.

S3 (and every later feature) INSTANTIATES this template rather than editing it:

    cp qa/gates/feature_behaviour_gate.py qa/gates/stats_gate.py
    # edit the SPEC block below: request.path, expect_status, headers_present,
    # json_assertions (each: {"id", "path", and one of "equals" / "type":"int" /
    # "exists": true})
    # then register it in qa/gates/registry.yaml as a new gate entry pointing at
    # qa/gates/stats_gate.py with its own pass-bar-<TASK>.yaml.

For an ad-hoc run you may instead point $FEATURE_GATE_SPEC at a JSON file
carrying the SPEC. The gate honours the F4 contract via _gatelib (exit 0 = pass;
non-zero enumerates failing assertions as the JSON results envelope). Target
base URL comes from $API_TEST_BASE_URL (default http://localhost:8901).

Example instantiation for Rich's §7 /stats feature (GET /stats -> JSON with
service:str, requests_served:int, first_request_at:str|null):

    SPEC = {
        "gate_id": "stats",
        "base_url_env": "API_TEST_BASE_URL",
        "default_base_url": "http://localhost:8901",
        "request": {"method": "GET", "path": "/stats"},
        "expect_status": 200,
        "headers_present": ["x-correlation-id", "x-api-version"],
        "json_assertions": [
            {"id": "stats::service_present",  "path": "service",          "exists": True},
            {"id": "stats::requests_is_int",  "path": "requests_served",  "type": "int"},
            {"id": "stats::first_at_present", "path": "first_request_at", "exists": True},
        ],
    }
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _gatelib  # noqa: E402

# --- EDIT THIS BLOCK WHEN INSTANTIATING -----------------------------------
SPEC = {
    "gate_id": "feature-behaviour",
    "base_url_env": "API_TEST_BASE_URL",
    "default_base_url": "http://localhost:8901",
    "request": {"method": "GET", "path": "/REPLACE_ME"},
    "expect_status": 200,
    "headers_present": ["x-correlation-id", "x-api-version"],
    "json_assertions": [
        # {"id": "feature::field_present", "path": "some_field", "exists": True},
        # {"id": "feature::field_value",   "path": "some_field", "equals": "expected"},
        # {"id": "feature::count_is_int",  "path": "count",      "type": "int"},
    ],
}
# --------------------------------------------------------------------------

if __name__ == "__main__":
    override = os.environ.get("FEATURE_GATE_SPEC")
    spec = json.loads(open(override, encoding="utf-8").read()) if override else SPEC
    if spec["request"]["path"] == "/REPLACE_ME":
        # Honest "template not instantiated" — never a vacuous green.
        print(json.dumps({"assertions": [{
            "id": "feature-behaviour::not_instantiated",
            "status": "fail",
            "observed": "request.path is the unedited placeholder /REPLACE_ME",
            "expected": "instantiate the SPEC (endpoint + assertions) before registering this gate",
        }]}))
        sys.exit(1)
    _gatelib.run_spec(spec)
