"""Send a sample payload to the webhook, poll the task, print the submitted result.

Usage:
    python scripts/send_sample.py samples/claim_valid.json
    python scripts/send_sample.py samples/*.json --api http://localhost:8000 --mocks http://localhost:9000
"""
import argparse
import json
import os
import sys
import time

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--mocks", default="http://localhost:9000")
    ap.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", "change-me-webhook-secret"))
    args = ap.parse_args()
    headers = {"X-Webhook-Secret": args.secret}

    for path in args.files:
        payload = json.load(open(path))
        print(f"\n=== {path} ===")
        r = httpx.post(f"{args.api}/webhook/validate", json=payload, headers=headers)
        print(f"POST /webhook/validate -> {r.status_code} {r.text}")
        if r.status_code != 202:
            continue
        task_id = r.json()["task_id"]

        seen = None
        for _ in range(120):
            t = httpx.get(f"{args.api}/tasks/{task_id}", headers=headers).json()
            key = (t["status"], t["current_step"])
            if key != seen:
                print(f"GET /tasks/{task_id[:8]}… -> status={t['status']:<10} step={t['current_step']}")
                seen = key
            if t["status"] in ("completed", "failed"):
                break
            time.sleep(0.25)

        events = httpx.get(f"{args.mocks}/status/{task_id}")
        if events.status_code == 200 and events.json():
            print("Status timeline (pushed to Status API):")
            for e in events.json():
                print(f"  {e['updated_at'][11:23]}  {e['status']:<10} {e['current_step']}")

        fid = payload["feedback_id"]
        res = httpx.get(f"{args.mocks}/results/{fid}")
        if res.status_code == 200:
            body = {k: v for k, v in res.json().items() if not k.startswith("_")}
            print("Result received by Result API:")
            print(json.dumps(body, indent=2))
        else:
            print(f"No result at Result API ({res.status_code})", file=sys.stderr)


if __name__ == "__main__":
    main()
