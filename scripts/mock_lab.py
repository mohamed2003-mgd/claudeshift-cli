from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_HOME = ROOT / ".sandbox_home"
SANDBOX_VAULT = ROOT / ".sandbox_vault"


def build_sandbox(reset: bool = False) -> None:
    if reset:
        for target in (SANDBOX_HOME, SANDBOX_VAULT):
            if target.exists():
                shutil.rmtree(target)

    SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
    SANDBOX_VAULT.mkdir(parents=True, exist_ok=True)

    claude_dir = SANDBOX_HOME / ".claude"
    project_dir = claude_dir / "projects" / "demo-backend-api"
    project_dir.mkdir(parents=True, exist_ok=True)

    (claude_dir / "settings.json").write_text(
        json.dumps(
            {"theme": "dark", "model": "claude-sonnet-5-5", "permissions": {"allow": ["Bash(pytest:*)"]}},
            indent=2,
        ),
        encoding="utf-8",
    )

    (SANDBOX_HOME / ".claude.json").write_text(
        json.dumps(
            {
                "numStartups": 42,
                "oauthAccount": {
                    "emailAddress": "dev.work@example.com",
                    "organizationName": "Work Engineering Org",
                },
                "mcpServers": {
                    "postgres-local": {"command": "npx", "args": ["-y", "@mcp/postgres"]},
                    "github": {"command": "npx", "args": ["-y", "@mcp/github"]},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    now = datetime.now(timezone.utc)
    jsonl_events = []
    for i in range(6):
        ts = (now - timedelta(minutes=(6 - i) * 5)).isoformat()
        jsonl_events.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "sessionId": "sess-mock-001",
                    "message": {
                        "id": f"msg-init-{i}",
                        "model": "claude-sonnet-5-5" if i % 2 else "claude-opus-5-5",
                        "usage": {
                            "input_tokens": 3000 + i * 500,
                            "output_tokens": 1200 + i * 300,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 5000,
                        },
                    },
                }
            )
        )
    (project_dir / "sess-mock-001.jsonl").write_text("\n".join(jsonl_events) + "\n", encoding="utf-8")
    print(f"[+] Sandbox ready at: {SANDBOX_HOME}")


def stream_live_events(steps: int = 12, delay_sec: float = 2.0) -> None:
    """Append realistic Claude Code events every 2 seconds to test 'claudeshift monitor' live."""
    project_dir = SANDBOX_HOME / ".claude" / "projects" / "live-billing-service"
    project_dir.mkdir(parents=True, exist_ok=True)
    log_file = project_dir / "sess-live-stream.jsonl"

    print(f"[*] Streaming {steps} live usage bursts into {log_file} every {delay_sec}s...")
    for i in range(1, steps + 1):
        event = {
            "type": "assistant",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": "sess-live-stream",
            "message": {
                "id": f"msg-live-{int(time.time())}-{i}",
                "model": "claude-opus-5-5" if i % 2 else "claude-fable-5-2",
                "usage": {
                    "input_tokens": 8000,
                    "output_tokens": 2500,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 12000,
                },
            },
        }
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        print(f"  [{i}/{steps}] Appended live burst ({event['message']['model']})")
        time.sleep(delay_sec)
    print("[+] Live stream finished.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create isolated AI CLI sandbox and live stream simulator.")
    parser.add_argument("--reset", action="store_true", help="Wipe and recreate sandbox files.")
    parser.add_argument("--stream", action="store_true", help="Stream live usage bursts for monitor testing.")
    args = parser.parse_args()
    if args.stream:
        stream_live_events()
    else:
        build_sandbox(reset=args.reset)