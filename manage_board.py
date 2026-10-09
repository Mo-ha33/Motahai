#!/usr/bin/env python3
"""
manage_board.py - Autonomous Task Board & Git Synchronization Manager
Enforces the Strict Multi-Model Role Framework:
- Opus 5.5: Orchestrator, Reviewer & Planner (No code writing)
- Sonnet 5.5 / Gemini 3.8 / Haiku 5.5: Code Implementers
- Gemini 3.8: Inspector, GitHub & Log Checker
"""

import sys
import os
import re
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

BOARD_FILE = Path(__file__).parent / "PROJECT_BOARD.md"

VALID_STATUSES = ["BACKLOG", "IN_PROGRESS", "REVIEW", "DONE"]
VALID_ROLES = ["Opus 5.5", "Sonnet 5.5", "Gemini 3.8", "Haiku 5.5"]
VALID_PRIORITIES = ["P0", "P1", "P2"]

def load_board():
    if not BOARD_FILE.exists():
        return ""
    return BOARD_FILE.read_text(encoding="utf-8")

def parse_tasks(content):
    tasks = []
    # Match markdown table rows: | ID | Title | Assignee | Priority | Status | Tags | Notes |
    table_pattern = re.compile(
        r"^\|\s*([A-Z0-9\-]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*(P[0-2])\s*\|\s*([A-Z_]+)\s*\|\s*([^|]+)\s*\|\s*([^|]*)\s*\|",
        re.MULTILINE
    )
    for m in table_pattern.finditer(content):
        tid, title, role, priority, status, tags, notes = [g.strip() for g in m.groups()]
        if tid in ("TASK_ID", "---", ":---"):
            continue
        tasks.append({
            "id": tid,
            "title": title,
            "role": role,
            "priority": priority,
            "status": status,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "notes": notes
        })
    return tasks

def save_board(content):
    BOARD_FILE.write_text(content, encoding="utf-8")

def update_task_status(task_id, new_status, note=None):
    if new_status not in VALID_STATUSES:
        print(f"Error: Invalid status '{new_status}'. Allowed: {VALID_STATUSES}")
        sys.exit(1)
        
    content = load_board()
    lines = content.splitlines()
    updated = False
    
    for i, line in enumerate(lines):
        if line.startswith(f"| {task_id} ") or line.startswith(f"|{task_id} "):
            parts = [p.strip() for p in line.split("|")]
            # parts[0] is empty, parts[1]=ID, parts[2]=Title, parts[3]=Role, parts[4]=Pri, parts[5]=Status, parts[6]=Tags, parts[7]=Notes
            if len(parts) >= 8:
                parts[5] = new_status
                if note:
                    parts[7] = f"{note} (updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
                lines[i] = f"| {' | '.join(parts[1:8])} |"
                updated = True
                break
                
    if not updated:
        print(f"Error: Task {task_id} not found on the board.")
        sys.exit(1)
        
    save_board("\n".join(lines) + "\n")
    print(f"✅ Task {task_id} updated to {new_status}")

def git_sync(message):
    try:
        subprocess.run(["git", "add", str(BOARD_FILE), "requirements.txt"], check=True)
        subprocess.run(["git", "commit", "-m", f"board: {message}"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("🚀 Successfully synced PROJECT_BOARD.md to GitHub main!")
    except subprocess.CalledProcessError as e:
        print(f"Git sync failed: {e}")

def list_tasks(status=None, tag=None):
    tasks = parse_tasks(load_board())
    filtered = tasks
    if status:
        filtered = [t for t in filtered if t["status"].upper() == status.upper()]
    if tag:
        filtered = [t for t in filtered if tag.lower() in [x.lower() for x in t["tags"]]]
        
    print(f"\n📋 Project Tasks ({len(filtered)} items):")
    print(f"{'ID':<10} {'PRI':<5} {'STATUS':<12} {'ASSIGNEE':<12} {'TAGS':<20} {'TITLE'}")
    print("-" * 80)
    for t in filtered:
        tags_str = ",".join(t["tags"])
        print(f"{t['id']:<10} {t['priority']:<5} {t['status']:<12} {t['role']:<12} {tags_str:<20} {t['title']}")

def print_stats():
    tasks = parse_tasks(load_board())
    counts = {s: 0 for s in VALID_STATUSES}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        
    total = len(tasks)
    done = counts.get("DONE", 0)
    pct = (done / total * 100) if total else 0
    print("\n📊 Board Statistics:")
    print(f"Total Tasks: {total} | Completed: {done} ({pct:.1f}%)")
    for s, c in counts.items():
        print(f"  - {s:<12}: {c}")

def main():
    parser = argparse.ArgumentParser(description="Manage Motahai Project Board")
    subparsers = parser.add_subparsers(dest="command")
    
    # list
    list_p = subparsers.add_parser("list")
    list_p.add_argument("--status", help="Filter by status (BACKLOG, IN_PROGRESS, REVIEW, DONE)")
    list_p.add_argument("--tag", help="Filter by tag")
    
    # update
    up_p = subparsers.add_parser("update")
    up_p.add_argument("id", help="Task ID")
    up_p.add_argument("status", help="New status (BACKLOG, IN_PROGRESS, REVIEW, DONE)")
    up_p.add_argument("--note", help="Update note")
    
    # sync
    sync_p = subparsers.add_parser("sync")
    sync_p.add_argument("-m", "--message", default="update task board", help="Commit message")
    
    # stats
    subparsers.add_parser("stats")
    
    args = parser.parse_args()
    if args.command == "list":
        list_tasks(args.status, args.tag)
    elif args.command == "update":
        update_task_status(args.id, args.status, args.note)
    elif args.command == "sync":
        git_sync(args.message)
    elif args.command == "stats":
        print_stats()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
