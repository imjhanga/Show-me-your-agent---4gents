"""Local demo server: clinic dashboard plus a mock WhatsApp panel.

Standard library only, so there is no build step, no package install and no
network dependency. The browser polls /api/state; stdlib has no websockets and
a one-second poll is more than enough for a demo on one laptop.

Binds to localhost. It serves fictional data and has no authentication, so it
must not be exposed to a network.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import bridge
from .audit import AuditLog
from .clock import DEFAULT_CLINIC_NOW, parse_clinic_now
from .conversation import (
    AWAITING_RESPONSE,
    ESCALATED,
    PENDING_APPROVAL,
    ConversationStore,
    KillSwitchEngaged,
)
from .llm import AUTO, OFFLINE, LLMClient
from .ranking import RankingWeights
from .risk import RuleTable
from .scan import run_scan

WEB_ROOT = Path(__file__).parent / "web"
WORKLIST_LIMIT = 60


class DemoState:
    """Everything the dashboard renders, held in memory for one demo run."""

    def __init__(
        self,
        clinic_now: datetime,
        db_path: Path,
        use_llm: bool,
        offline: bool,
    ) -> None:
        self.clinic_now = clinic_now
        self.db_path = db_path
        self.use_llm = use_llm
        self.client = LLMClient(mode=OFFLINE if offline else AUTO)
        self.audit = AuditLog()
        self.rules = RuleTable.load()
        self.weights = RankingWeights.load()
        self.store = ConversationStore(
            clinic_now, self.audit, client=self.client, use_llm=use_llm
        )
        self.scan = None
        self.cases: dict[str, dict[str, Any]] = {}

    def rescan(self) -> None:
        self.scan = run_scan(
            db_path=self.db_path,
            clinic_now=self.clinic_now,
            rules=self.rules,
            weights=self.weights,
            audit=self.audit,
        )
        document = bridge.build_document(self.db_path, self.clinic_now)
        self.cases = {case["case_id"]: case for case in document["cases"]}
        for entry in self.scan.worklist[:WORKLIST_LIMIT]:
            self.store.adopt(entry, self.cases[entry["case_id"]])

    def reset(self) -> None:
        self.store.reset()
        self.rescan()

    def snapshot(self) -> dict[str, Any]:
        assert self.scan is not None
        conversations = {c.case_id: c.to_dict() for c in self.store.conversations.values()}
        worklist = []
        for entry in self.scan.worklist[:WORKLIST_LIMIT]:
            row = dict(entry)
            row["conversation"] = conversations.get(entry["case_id"])
            worklist.append(row)
        return {
            "clinic_now": self.clinic_now.isoformat(),
            "kill_switch": self.store.kill_switch,
            "offline": self.client.mode == OFFLINE,
            "use_llm": self.use_llm,
            "totals": self.scan.totals,
            "policy_versions": self.scan.policy_versions,
            "worklist": worklist,
            "refusal_summary": self.scan.refusal_summary,
            "refusals": self.scan.refusals[:40],
            "approvals": [c.to_dict() for c in self.store.by_state(PENDING_APPROVAL)],
            "escalations": [c.to_dict() for c in self.store.by_state(ESCALATED)],
            "active": [c.to_dict() for c in self.store.by_state(AWAITING_RESPONSE)],
            "workflow": self.store.summary(),
            "audit": self.audit.read(limit=40)[::-1],
        }


class Handler(BaseHTTPRequestHandler):
    state: DemoState

    def log_message(self, *args: Any) -> None:  # noqa: A003
        return  # Keep the console clear during a demo.

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _file(self, name: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if not path.is_file() or WEB_ROOT.resolve() not in path.parents:
            self._send(404, b"not found", "text/plain")
            return
        types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
        self._send(
            200,
            path.read_bytes(),
            types.get(path.suffix, "application/octet-stream") + "; charset=utf-8",
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._file("index.html")
        elif self.path == "/api/state":
            self._json(self.state.snapshot())
        elif self.path.startswith("/static/"):
            self._file(self.path[len("/static/") :])
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._body()
            case_id = payload.get("case_id", "")
            store = self.state.store

            if self.path == "/api/prepare":
                store.prepare(case_id, self.state.cases[case_id])
            elif self.path == "/api/approve":
                store.approve(case_id)
            elif self.path == "/api/reject":
                store.reject(case_id, payload.get("reason", "Declined by staff"))
            elif self.path == "/api/reply":
                store.receive_reply(case_id, payload.get("text", ""))
            elif self.path == "/api/kill-switch":
                store.kill_switch = bool(payload.get("engaged"))
                self.state.audit.append(
                    "kill_switch.changed", engaged=store.kill_switch, actor="staff"
                )
            elif self.path == "/api/reset":
                self.state.reset()
            else:
                self._send(404, b"not found", "text/plain")
                return
        except KillSwitchEngaged as error:
            self._json({"error": str(error)}, status=409)
            return
        except KeyError as error:
            self._json({"error": f"unknown case {error}"}, status=404)
            return
        except (ValueError, TypeError) as error:
            self._json({"error": str(error)}, status=400)
            return

        self._json(self.state.snapshot())


def serve(
    port: int = 8000,
    db_path: Path = bridge.DEFAULT_DB,
    clinic_now: datetime = DEFAULT_CLINIC_NOW,
    use_llm: bool = True,
    offline: bool = False,
) -> None:
    state = DemoState(clinic_now, db_path, use_llm, offline)
    print("Scanning the patient database...")
    state.rescan()
    totals = state.scan.totals if state.scan else {}
    print(
        f"  {totals.get('requirements_scanned')} requirements across "
        f"{totals.get('patients_scanned')} patients; "
        f"{totals.get('worklist')} on the worklist, {totals.get('excluded')} refused."
    )

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  Dashboard: http://127.0.0.1:{port}/")
    print("  Fictional data only. Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the follow-up agent demo.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=bridge.DEFAULT_DB)
    parser.add_argument("--clinic-now", default=None)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Replay cached model responses only; never open a socket.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use approved templates and the rule-based classifier only.",
    )
    args = parser.parse_args(argv)
    serve(
        port=args.port,
        db_path=args.db,
        clinic_now=parse_clinic_now(args.clinic_now),
        use_llm=not args.no_llm,
        offline=args.offline,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
