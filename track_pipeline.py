"""
Audio Processing Pipeline — Real-Time Terminal Tracker & CLI Controller.
Enables monitoring the audio pipeline progress in real-time in any separate terminal.

Usage:
  python track_pipeline.py                 # Live auto-refreshing dashboard
  python track_pipeline.py --watch         # Live event log stream
  python track_pipeline.py --run <rec_id>  # Execute pipeline for a record directly with live logs
  python track_pipeline.py --list          # Print current jobs snapshot and exit
"""

import sys
import os
import time
import argparse
from backend.database import get_connection
from datetime import datetime

# Windows console UTF-8 fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DB_PATH = os.path.join(os.path.dirname(__file__), "audio_workflow.db")

STEP_DEFS = [
    ("validate", "Validate"),
    ("convert",  "Convert"),
    ("denoise",  "Denoise"),
    ("vad",      "VAD"),
    ("asr",      "ASR"),
    ("zip",      "Zip")
]

# ANSI color codes
CLR_RESET   = "\033[0m"
CLR_BOLD    = "\033[1m"
CLR_CYAN    = "\033[36m"
CLR_GREEN   = "\033[32m"
CLR_YELLOW  = "\033[33m"
CLR_RED     = "\033[31m"
CLR_MAGENTA = "\033[35m"
CLR_GRAY    = "\033[90m"
CLR_BG_DARK = "\033[48;5;236m"


def get_jobs_from_db():
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            SELECT pj.*, r.status as record_status, r.description as record_desc,
                   r.before_processing_duration as raw_dur
            FROM pipeline_jobs pj
            LEFT JOIN records r ON pj.record_id = r.id
            ORDER BY 
              CASE pj.stage 
                WHEN 'DOWNLOADING' THEN 1 
                WHEN 'PROCESSING' THEN 2 
                WHEN 'EXTRACTING' THEN 3 
                WHEN 'QUEUED' THEN 4
                WHEN 'DONE' THEN 5 
                ELSE 6 
              END,
              pj.updated_at DESC
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        return []


def format_bar(pct, width=28):
    pct = max(0, min(100, int(pct or 0)))
    filled = int(width * (pct / 100.0))
    bar = "=" * filled + (">" if filled < width else "") + " " * (width - filled - (1 if filled < width else 0))
    return f"[{bar}] {pct:>3}%"


def format_stepper(step_idx, is_done, is_running):
    step_idx = int(step_idx or 0)
    items = []
    for i, (sid, label) in enumerate(STEP_DEFS):
        if is_done or i < step_idx:
            items.append(f"{CLR_GREEN}✓ {label}{CLR_RESET}")
        elif i == step_idx and is_running:
            items.append(f"{CLR_CYAN}{CLR_BOLD}▶ {label}{CLR_RESET}")
        else:
            items.append(f"{CLR_GRAY}○ {label}{CLR_RESET}")
    return "  ->  ".join(items)


def clear_screen():
    # Clear screen without flickering
    os.system("cls" if os.name == "nt" else "clear")


def render_dashboard():
    jobs = get_jobs_from_db()
    now_str = datetime.now().strftime("%H:%M:%S")

    running_jobs = [j for j in jobs if j.get("stage") in ("DOWNLOADING", "PROCESSING", "EXTRACTING")]
    done_jobs = [j for j in jobs if j.get("stage") == "DONE"]
    failed_jobs = [j for j in jobs if j.get("stage") == "FAILED"]

    out = []
    out.append(f"{CLR_BOLD}{CLR_CYAN}========================================================================================{CLR_RESET}")
    out.append(f"{CLR_BOLD}  🎧 AUDIO PROCESSING PIPELINE — LIVE TERMINAL TRACKER{CLR_RESET}   {CLR_GRAY}[{now_str}]{CLR_RESET}")
    out.append(f"  Endpoint: http://111.118.177.236:8008  |  DB: PostgreSQL (tts_research)")
    out.append(f"{CLR_BOLD}{CLR_CYAN}========================================================================================{CLR_RESET}")
    out.append(f"  {CLR_BOLD}Active Jobs: {CLR_YELLOW}{len(running_jobs)}{CLR_RESET}  |  {CLR_BOLD}Completed: {CLR_GREEN}{len(done_jobs)}{CLR_RESET}  |  {CLR_BOLD}Failed: {CLR_RED}{len(failed_jobs)}{CLR_RESET}  |  {CLR_BOLD}Total Tracked: {len(jobs)}{CLR_RESET}")
    out.append(f"{CLR_GRAY}----------------------------------------------------------------------------------------{CLR_RESET}")

    if not jobs:
        out.append(f"\n  {CLR_GRAY}No pipeline jobs registered yet. Run 'python track_pipeline.py --run <record_id>' or use Web UI.{CLR_RESET}\n")
    else:
        for job in jobs:
            rec_id = job.get("record_id")
            stage = job.get("stage") or "IDLE"
            is_running = stage in ("DOWNLOADING", "PROCESSING", "EXTRACTING")
            is_done = stage == "DONE"
            is_failed = stage == "FAILED"

            if is_done:
                badge = f"{CLR_GREEN}[DONE]{CLR_RESET}"
            elif stage == "DOWNLOADING":
                badge = f"{CLR_YELLOW}[DOWNLOADING]{CLR_RESET}"
            elif is_running:
                badge = f"{CLR_CYAN}{CLR_BOLD}[RUNNING]{CLR_RESET}"
            elif is_failed:
                badge = f"{CLR_RED}{CLR_BOLD}[FAILED]{CLR_RESET}"
            else:
                badge = f"{CLR_GRAY}[QUEUED]{CLR_RESET}"

            file_name = job.get("file_name") or f"Record #{rec_id}"
            speaker = job.get("speaker_id") or "Unknown Speaker"
            step_name = job.get("step_name") or "Initializing..."
            step_idx = job.get("step_index", 0)
            pct = job.get("progress_pct", 0)
            took = job.get("took_formatted") or (f"{job.get('elapsed_seconds', 0)}s" if is_running else "")
            seg_count = job.get("segments_count", 0)

            out.append(f"\n  {CLR_BOLD}Record #{rec_id:<4}{CLR_RESET} {file_name:<28}  {badge}  {CLR_GRAY}Speaker: {speaker}{CLR_RESET}")
            out.append(f"    Progress : {CLR_CYAN}{format_bar(pct)}{CLR_RESET}  {CLR_GRAY}({took}){CLR_RESET}")
            out.append(f"    Pipeline : {format_stepper(step_idx, is_done, is_running)}")
            
            if is_done:
                out.append(f"    Status   : {CLR_GREEN}Finished! {seg_count} segments extracted & ready in Validation Studio{CLR_RESET}")
            elif is_failed:
                out.append(f"    Status   : {CLR_RED}Error: {job.get('error') or step_name}{CLR_RESET}")
            else:
                out.append(f"    Status   : {CLR_YELLOW}{step_name}{CLR_RESET}")

    out.append(f"\n{CLR_GRAY}----------------------------------------------------------------------------------------{CLR_RESET}")
    out.append(f"  {CLR_GRAY}Press Ctrl + C to exit | Run 'python track_pipeline.py --help' for options{CLR_RESET}")
    return "\n".join(out)


def run_live_dashboard():
    clear_screen()
    try:
        while True:
            text = render_dashboard()
            # Position cursor at top-left
            sys.stdout.write("\033[H" + text + "\n")
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n{CLR_GRAY}Exited pipeline tracker.{CLR_RESET}\n")


def run_watch_stream():
    print(f"\n{CLR_BOLD}{CLR_CYAN}▶ Watching pipeline events stream in real time... (Press Ctrl+C to stop){CLR_RESET}\n")
    last_states = {}
    try:
        while True:
            jobs = get_jobs_from_db()
            for j in jobs:
                rid = j.get("record_id")
                pct = j.get("progress_pct", 0)
                step = j.get("step_name")
                stage = j.get("stage")
                key = (stage, pct, step)
                if rid not in last_states or last_states[rid] != key:
                    last_states[rid] = key
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Record #{rid:<3} [{pct:>3}%] {stage:<11} -> {step}", flush=True)
            time.sleep(0.8)
    except KeyboardInterrupt:
        print(f"\n{CLR_GRAY}Watch stream stopped.{CLR_RESET}")


def run_cli_pipeline(record_id: int):
    import asyncio
    from backend.pipeline_service import run_full_pipeline

    print(f"\n{CLR_BOLD}{CLR_CYAN}========================================================================{CLR_RESET}")
    print(f"{CLR_BOLD}  🚀 Triggering Pipeline Directly for Record #{record_id} with Live Output{CLR_RESET}")
    print(f"{CLR_BOLD}{CLR_CYAN}========================================================================{CLR_RESET}\n")

    async def _exec():
        res = await run_full_pipeline(record_id=record_id, force_redownload=True)
        if res.get("success"):
            print(f"\n{CLR_GREEN}{CLR_BOLD}✅ Record #{record_id} pipeline completed successfully!{CLR_RESET}")
            print(f"   Segments extracted: {len(res.get('segments', []))}")
            print(f"   Now visible in the Web UI: http://localhost:8088 (Approved Dataset / Track Pipeline)")
        else:
            print(f"\n{CLR_RED}{CLR_BOLD}❌ Pipeline failed for Record #{record_id}: {res.get('error')}{CLR_RESET}")

    asyncio.run(_exec())


def main():
    parser = argparse.ArgumentParser(description="Real-time Audio Pipeline Tracker & Controller")
    parser.add_argument("--watch", "-w", action="store_true", help="Watch real-time event logs as they stream")
    parser.add_argument("--run", "-r", type=int, metavar="RECORD_ID", help="Execute pipeline for a record with full live logs")
    parser.add_argument("--list", "-l", action="store_true", help="Print current snapshot of all jobs and exit")

    args = parser.parse_args()

    if args.run:
        run_cli_pipeline(args.run)
    elif args.watch:
        run_watch_stream()
    elif args.list:
        print(render_dashboard())
    else:
        run_live_dashboard()


if __name__ == "__main__":
    main()
