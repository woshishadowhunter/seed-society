"""Transparent logging proxy for api.deepseek.com.

Captures the EXACT request the harness/mneme sends (headers + body) to a log
file, then forwards it upstream and streams the response back. Purpose: settle
by observation whether mneme's autoDream call really carries thinking /
reasoning_effort / max_tokens as assumed.

Run:  python integrations/dsh/probe/llm_proxy.py --port 8799
Then point the profile's llm-deepseek baseURL at http://127.0.0.1:8799 and
trigger a dream. Logs land in llm_proxy_capture.jsonl.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM = "https://api.deepseek.com"
CAPTURE = Path(__file__).with_name("llm_proxy_capture.jsonl")

_INTERESTING = ("thinking", "reasoning_effort", "max_tokens", "model", "stream")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep stdout clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._forward()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        summary: dict[str, object] = {"path": self.path, "method": self.command}
        try:
            payload = json.loads(body.decode("utf-8"))
            summary["request"] = {k: payload.get(k) for k in _INTERESTING if k in payload}
            summary["messages"] = len(payload.get("messages") or [])
            summary["prompt_chars"] = sum(
                len(str(m.get("content"))) for m in (payload.get("messages") or [])
            )
        except Exception as error:  # non-JSON or chunked upload
            summary["body_error"] = f"{type(error).__name__}"
            summary["body_chars"] = len(body)

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() in {"authorization", "content-type", "accept"}
        }
        request = urllib.request.Request(
            UPSTREAM + self.path, data=body or None, headers=headers, method=self.command
        )
        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=900, context=context) as response:
                summary["status"] = response.status
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in {"content-type", "transfer-encoding", "connection"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                total = 0
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    self.wfile.write(chunk)
                summary["response_bytes"] = total
        except Exception as error:
            summary["error"] = f"{type(error).__name__}: {error}"
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
            except Exception:
                pass
        finally:
            with CAPTURE.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(summary, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"probe proxy on http://127.0.0.1:{args.port} -> {UPSTREAM}", flush=True)
    print(f"capture file: {CAPTURE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
