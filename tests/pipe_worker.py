"""Invented native-process substitute; exercises the actual supervisor pipe."""

import json
import os
import sys
import threading
from pathlib import Path

config = json.loads(sys.stdin.readline())
root = Path(config["root"])
print(json.dumps({"ready": True}), flush=True)


downloads = {}


def reply(message):
    ident, method = message["id"], message["method"]
    if method == "downloadByStrategy":
        payload = json.loads(message["args"])
        item = payload["item_id"]
        cancelled = threading.Event()
        downloads[item] = cancelled
        for n in range(20):
            if item in {"progress", "cap"}:
                print(
                    json.dumps(
                        {
                            "event": {
                                "kind": 10,
                                "value": {
                                    "items": {
                                        item: {"bytes_received": n * 10, "status": "downloading"}
                                    }
                                },
                            }
                        }
                    ),
                    flush=True,
                )
            if cancelled.wait(0.03) and item != "hung":
                print(
                    json.dumps(
                        {"id": ident, "result": {"success": False, "error_type": "cancelled"}}
                    ),
                    flush=True,
                )
                return
        if item == "hung":
            return
        print(json.dumps({"id": ident, "result": {"success": True}}), flush=True)
        return
    if method == "cancelDownload":
        cancelled = downloads.get(message["args"]["item_id"])
        if cancelled:
            cancelled.set()
        (root / ("item-cancel-" + message["args"]["item_id"])).write_text("yes")
    if method == "crash":
        os._exit(1)
    if method == "hang":
        return
    if method == "noisy":
        sys.stderr.write("private-token https://private.example.test/path?grant=secret\n" * 2000)
        sys.stderr.flush()
    print(json.dumps({"id": ident, "result": message.get("args")}), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    if "cancel" in message:
        (root / "cancel-observed").write_text("yes")
        # No callback, deliberately. The parent must recover the owner on deadline.
    else:
        threading.Thread(target=reply, args=(message,), daemon=True).start()
