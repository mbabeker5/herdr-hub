#!/usr/bin/env python3
"""Snapshot and restore the whole Herdr layout: every workspace, tab, pane and agent.

Why this exists. Restarting the Herdr server (after `herdr update`, or a crash)
kills every pane process, so all the agents go with it. Rebuilding a dozen
workspaces by hand is slow and the session ids are easy to lose. Snapshot before
the restart, restore after, and every worker comes back in the same folder with
the same conversation.

Usage
    python3 /Users/mtalib/workspace_repos/herdr_layout.py snapshot
    python3 /Users/mtalib/workspace_repos/herdr_layout.py restore --dry-run
    python3 /Users/mtalib/workspace_repos/herdr_layout.py restore

    restore --fresh          start every agent with a clean context (no --resume)
    restore --only a,b,c     restore just these agent names
    restore --include-hub    also recreate the workspace this script runs in
    snapshot -o PATH         write somewhere other than the default

Default snapshot file: /Users/mtalib/.config/herdr/layout.json

What restore does per workspace: `workspace create` with the original label and
working directory, then `agent start` in the returned root pane, resuming that
agent's session. A workspace with more than one tab gets a `tab create` per extra
tab, each with its own agent. Everything runs with --no-focus so your focus does
not jump around, and with --permission-mode bypassPermissions, which is how these
workers already run.

Restore is additive. It never closes anything. Run it against a fresh Herdr, or
pass --only to bring back the few that died.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_FILE = Path.home() / ".config" / "herdr" / "layout.json"
START_TIMEOUT_MS = 90000
AGENT_ARGS = ["--permission-mode", "bypassPermissions"]


def herdr(*args, check=True):
    """Run a herdr CLI command and return its .result, or None on failure."""
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        if check:
            print(f"  ! herdr {' '.join(args)}\n    {proc.stderr.strip()[:300]}")
        return None
    try:
        return json.loads(proc.stdout)["result"]
    except (json.JSONDecodeError, KeyError):
        return None


def snapshot(path: Path) -> dict:
    """Read the live layout into a plain dict, ordered as the sidebar shows it."""
    workspaces = herdr("workspace", "list")["workspaces"]
    agents = {a["pane_id"]: a for a in herdr("agent", "list")["agents"]}

    out = []
    for w in workspaces:
        wid = w["workspace_id"]
        tabs = herdr("tab", "list", "--workspace", wid).get("tabs", [])
        panes = herdr("pane", "list", "--workspace", wid).get("panes", [])
        by_tab = {}
        for p in panes:
            by_tab.setdefault(p.get("tab_id"), []).append(p)

        tab_records = []
        for t in tabs:
            tid = t["tab_id"]
            pane_records = []
            for p in by_tab.get(tid, []):
                a = agents.get(p["pane_id"])
                pane_records.append({
                    "pane_id": p["pane_id"],
                    "cwd": p.get("cwd"),
                    "agent": None if not a else {
                        "name": a["name"],
                        "kind": a["agent"],
                        "session_id": (a.get("agent_session") or {}).get("value"),
                        "cwd": a.get("cwd"),
                    },
                })
            tab_records.append({
                "tab_id": tid,
                "label": t.get("label"),
                "panes": pane_records,
            })

        out.append({
            "workspace_id": wid,
            "label": w.get("label"),
            "number": w.get("number"),
            "tabs": tab_records,
        })

    data = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "workspaces": out,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n")
    return data


def describe(data: dict) -> None:
    n_ws = len(data["workspaces"])
    n_agents = sum(1 for w in data["workspaces"] for t in w["tabs"]
                   for p in t["panes"] if p["agent"])
    print(f"{n_ws} workspaces, {n_agents} agents, captured {data['captured_at']}")
    for w in data["workspaces"]:
        names = [p["agent"]["name"] for t in w["tabs"] for p in t["panes"] if p["agent"]]
        print(f"  {w['label']!r}: {', '.join(names) if names else 'no agents'}")


def start_agent(agent: dict, pane_id: str, fresh: bool, dry: bool) -> bool:
    cmd = ["agent", "start", agent["name"], "--kind", agent["kind"],
           "--pane", pane_id, "--timeout", str(START_TIMEOUT_MS), "--"]
    cmd += AGENT_ARGS
    if not fresh and agent.get("session_id"):
        cmd += ["--resume", agent["session_id"]]
    if dry:
        print("    herdr " + " ".join(cmd))
        return True
    res = herdr(*cmd)
    if res is None:
        print(f"    ! {agent['name']} did not come up. Pane {pane_id} is there; "
              f"start it by hand or re-run with --only {agent['name']}.")
        return False
    print(f"    started {agent['name']} in {pane_id}")
    return True


def restore(data: dict, fresh: bool, dry: bool, only: set, include_hub: bool) -> None:
    here = os.environ.get("HERDR_WORKSPACE_ID")
    ok = failed = 0

    for w in data["workspaces"]:
        if not include_hub and here and w["workspace_id"] == here:
            print(f"- {w['label']!r}: skipped, this script is running in it "
                  f"(use --include-hub to recreate it anyway)")
            continue

        wanted = [(t, p) for t in w["tabs"] for p in t["panes"]
                  if p["agent"] and (not only or p["agent"]["name"] in only)]
        if only and not wanted:
            continue
        if not wanted:
            continue

        first_cwd = next((p["cwd"] or p["agent"]["cwd"]
                          for t in w["tabs"] for p in t["panes"] if p["agent"]), None)
        print(f"- {w['label']!r}")

        if dry:
            print(f"    herdr workspace create --cwd {first_cwd} "
                  f"--label {w['label']!r} --no-focus")
            new_ws = "<new-ws>"
            root_pane = "<new-root-pane>"
        else:
            created = herdr("workspace", "create", "--cwd", first_cwd or str(Path.home()),
                            "--label", w["label"] or "", "--no-focus")
            if created is None:
                print("    ! could not create the workspace, skipping it")
                failed += len(wanted)
                continue
            new_ws = created["workspace"]["workspace_id"]
            root_pane = created["root_pane"]["pane_id"]

        # A new workspace arrives with one tab. Its agent goes in the root pane;
        # every later tab is created here.
        for i, (tab, pane) in enumerate(wanted):
            agent = pane["agent"]
            if i == 0:
                target = root_pane
            elif dry:
                print(f"    herdr tab create --workspace {new_ws} "
                      f"--cwd {pane['cwd']} --no-focus")
                target = "<new-tab-pane>"
            else:
                t = herdr("tab", "create", "--workspace", new_ws,
                          "--cwd", pane["cwd"] or first_cwd, "--no-focus")
                if t is None:
                    print(f"    ! could not create a tab for {agent['name']}")
                    failed += 1
                    continue
                target = t["root_pane"]["pane_id"]

            if start_agent(agent, target, fresh, dry):
                ok += 1
            else:
                failed += 1

    print(f"\n{ok} started, {failed} failed."
          + ("  (dry run, nothing was created)" if dry else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="write the current layout to a file")
    s.add_argument("-o", "--out", type=Path, default=DEFAULT_FILE)

    r = sub.add_parser("restore", help="recreate the layout from a file")
    r.add_argument("-f", "--file", type=Path, default=DEFAULT_FILE)
    r.add_argument("--fresh", action="store_true",
                   help="start agents with a clean context instead of --resume")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    r.add_argument("--only", default="", help="comma separated agent names")
    r.add_argument("--include-hub", action="store_true",
                   help="also recreate the workspace this script runs in")

    a = ap.parse_args()

    if a.cmd == "snapshot":
        data = snapshot(a.out)
        describe(data)
        print(f"\nwrote {a.out}")
        return 0

    if not a.file.is_file():
        print(f"No snapshot at {a.file}. Run the snapshot command first.")
        return 1
    data = json.loads(a.file.read_text())
    describe(data)
    print()
    restore(data, a.fresh, a.dry_run,
            {x.strip() for x in a.only.split(",") if x.strip()}, a.include_hub)
    return 0


if __name__ == "__main__":
    sys.exit(main())
