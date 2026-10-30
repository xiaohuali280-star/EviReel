"""E3 container smoke through the load balancer (deploy/e3/compose.yml): both API instances serve, one
login session works on either, and stopping one instance keeps the service up (requests move to the
other); after restart both serve again. Password from EVIREEL_NEW_PASSWORD (never printed).

  python scripts/e3/stack_smoke.py --url http://127.0.0.1:8000 --username ci-smoke \
      --stop "docker compose ... stop api-1" --start "docker compose ... start api-1"
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time

import httpx


def instances(client, n=12):
    seen = []
    for _ in range(n):
        response = client.get("/api/health")
        response.raise_for_status()
        seen.append(response.json().get("instance"))
    return seen


def wait_both(client, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if {"api-1", "api-2"} <= set(instances(client)):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--stop", required=True)
    parser.add_argument("--start", required=True)
    args = parser.parse_args()
    report = {}
    with httpx.Client(base_url=args.url, timeout=10) as client:
        assert wait_both(client), "both API instances must answer through the load balancer"
        report["balanced"] = sorted(set(instances(client)))
        login = client.post("/api/auth/login", json={"username": args.username, "password": os.environ["EVIREEL_NEW_PASSWORD"]})
        assert login.status_code == 200, login.text
        token = client.cookies.get("evireel_session")
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {token}"}
        served = {}
        for _ in range(12):     # the session row is in PostgreSQL: every instance accepts it
            for path in ("/api/auth/me", "/api/videos"):
                response = client.get(path, headers=headers)
                assert response.status_code == 200, response.text
                served[response.headers["x-evireel-instance"]] = True
        report["session_on"] = sorted(served)
        assert report["session_on"] == ["api-1", "api-2"], report
        subprocess.run(shlex.split(args.stop), check=True)
        outcomes = {}
        for _ in range(20):
            try:
                key = str(client.get("/api/videos", headers=headers).status_code)
            except httpx.HTTPError as exc:
                key = type(exc).__name__
            outcomes[key] = outcomes.get(key, 0) + 1
            time.sleep(0.1)
        report["one_instance_stopped"] = {"outcomes": outcomes, "instances": sorted(set(instances(client)))}
        assert report["one_instance_stopped"]["instances"] == ["api-2"] and outcomes == {"200": 20}, report
        subprocess.run(shlex.split(args.start), check=True)
        assert wait_both(client), "the restarted instance must rejoin"
        report["after_restart"] = sorted(set(instances(client)))
        assert client.get("/api/auth/me", headers=headers).status_code == 200
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
