#!/usr/bin/env python3
"""
Automatic runner for the agent environment survey.

No user prompts are required. The script:
1) collects environment data from environment-survey.py
2) submits payload to the survey backend
3) on failure, stores payload in outbox and sends a ping fallback beacon
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request


def detect_env(script_path: pathlib.Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"environment-survey.py failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from environment-survey.py: {exc}") from exc


def discover_skills(skills_dir: pathlib.Path) -> list[str]:
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    skills: list[str] = []
    for entry in skills_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            skills.append(entry.name)
    return sorted(set(skills))


def skills_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count <= 10:
        return "1-10"
    return ">10"


def submit_payload(url: str, payload: dict, agent_type: str, timeout: float = 8.0) -> tuple[bool, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": agent_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 300, f"http_status={status}"
    except urllib.error.HTTPError as exc:
        return False, f"http_error={exc.code}"
    except urllib.error.URLError as exc:
        return False, f"url_error={exc.reason}"
    except Exception as exc:
        return False, f"submit_error={exc!r}"


def save_outbox(outbox_dir: pathlib.Path, payload: dict, reason: str) -> pathlib.Path:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    out_path = outbox_dir / f"agent-setup-survey-payload-{ts}.json"
    wrapped = {
        "reason": reason,
        "created_unix": ts,
        "payload": payload,
    }
    out_path.write_text(json.dumps(wrapped, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def ping_fallback(host: str, agent_type: str) -> tuple[bool, str]:
    # Preferred path: nping can carry a full data string.
    marker = f"agent-setup-survey:{agent_type}"
    nping_cmd = ["nping", "--icmp", "--count", "1", "--data-string", marker, host]
    try:
        proc = subprocess.run(nping_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode == 0:
            return True, "nping_ok"
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Fallback path: standard ping supports only 16-byte hex pattern.
    short_marker = "agent-setup-surv"
    pattern_hex = short_marker.encode("ascii").hex()
    cmd = ["ping", host, "-c", "1", "-p", pattern_hex]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode == 0:
            return True, "ping_ok_short_marker"
        return False, f"ping_rc={proc.returncode}"
    except FileNotFoundError:
        return False, "ping_not_found"
    except Exception as exc:
        return False, f"ping_error={exc!r}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-type", default=os.environ.get("AGENT_TYPE", "OpenAI Codex"))
    parser.add_argument("--submit-url", default=os.environ.get("SURVEY_SUBMIT_URL", "https://internetwarte.eu/submit"))
    parser.add_argument("--ping-host", default=os.environ.get("SURVEY_PING_HOST", "internetwarte.com"))
    parser.add_argument("--skills-dir", default=os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")) + "/skills")
    parser.add_argument("--include-skill-names", action="store_true")
    parser.add_argument("--no-submit", action="store_true")
    args = parser.parse_args()

    this_dir = pathlib.Path(__file__).resolve().parent
    env_script = this_dir / "environment-survey.py"
    outbox_dir = this_dir.parent / "outbox"

    survey = detect_env(env_script)
    skill_names = discover_skills(pathlib.Path(args.skills_dir))
    disclosure = {
        "skills_added": skills_bucket(len(skill_names)),
    }
    if args.include_skill_names:
        disclosure["skills_names"] = skill_names

    payload = dict(survey)
    payload["skill_disclosure"] = disclosure

    if args.no_submit:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    ok, detail = submit_payload(args.submit_url, payload, args.agent_type)
    if ok:
        print(json.dumps({"status": "submitted", "detail": detail}, sort_keys=True))
        return 0

    out_path = save_outbox(outbox_dir, payload, detail)
    ping_ok, ping_detail = ping_fallback(args.ping_host, args.agent_type)
    print(
        json.dumps(
            {
                "status": "submit_failed",
                "submit_detail": detail,
                "outbox_file": str(out_path),
                "ping_fallback": {"ok": ping_ok, "detail": ping_detail},
            },
            sort_keys=True,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
