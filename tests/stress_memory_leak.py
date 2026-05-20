"""
Memory Leak & RAM Stress Test for stealth-chrome-devtools-mcp
=============================================================

Spawns multiple browser instances, navigates to pages that generate
network traffic, then closes them — tracking PID + RSS at each stage.

Usage:
    python tests/stress_memory_leak.py [--instances N] [--rounds N]

Requires: psutil, nodriver (already project deps)
"""

import argparse
import asyncio
import gc
import os
import sys
import time

import psutil

# Add the embedded source to path so we can import directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "stealth_chrome_devtools_mcp", "embedded"))

from browser_manager import BrowserManager
from network_interceptor import NetworkInterceptor
from dynamic_hook_system import dynamic_hook_system
from persistent_storage import persistent_storage
from debug_logger import debug_logger
from models import BrowserOptions

# Enable debug logging so the DebugLogger lists accumulate (leak vector)
debug_logger.enable()

HEAVY_PAGE = "https://en.wikipedia.org/wiki/Main_Page"
PROCESS = psutil.Process(os.getpid())


def rss_mb() -> float:
    """Current RSS of this Python process in MB."""
    return PROCESS.memory_info().rss / (1024 * 1024)


def collect_child_pids() -> list[dict]:
    """Snapshot of all child processes (Chrome) with their RSS."""
    children = []
    for child in PROCESS.children(recursive=True):
        try:
            children.append({
                "pid": child.pid,
                "name": child.name(),
                "rss_mb": child.memory_info().rss / (1024 * 1024),
                "status": child.status(),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return children


def snapshot(label: str) -> dict:
    """Take a labeled memory snapshot."""
    gc.collect()
    children = collect_child_pids()
    total_child_rss = sum(c["rss_mb"] for c in children)
    info = {
        "label": label,
        "timestamp": time.time(),
        "python_rss_mb": round(rss_mb(), 2),
        "child_count": len(children),
        "child_total_rss_mb": round(total_child_rss, 2),
        "combined_rss_mb": round(rss_mb() + total_child_rss, 2),
        "children": children,
    }
    return info


def print_snapshot(s: dict):
    print(f"\n{'='*60}")
    print(f"  {s['label']}")
    print(f"{'='*60}")
    print(f"  Python RSS:       {s['python_rss_mb']:>8.1f} MB")
    print(f"  Child processes:  {s['child_count']:>8d}")
    print(f"  Child total RSS:  {s['child_total_rss_mb']:>8.1f} MB")
    print(f"  Combined RSS:     {s['combined_rss_mb']:>8.1f} MB")
    if s["children"]:
        print(f"  Child PIDs:")
        for c in s["children"]:
            print(f"    PID {c['pid']:>8d}  {c['name']:<25s}  {c['rss_mb']:>7.1f} MB  [{c['status']}]")


def print_internal_state():
    """Print sizes of all singleton state dicts to detect accumulation."""
    print(f"\n--- Internal State Sizes ---")
    # persistent_storage
    data = persistent_storage._data
    print(f"  persistent_storage._data keys:    {len(data)}")
    print(f"  persistent_storage instances:     {len(data.get('instances', {}))}")
    prog = persistent_storage.get("progressive_elements", {})
    print(f"  progressive_elements stored:      {len(prog)}")

    # debug_logger
    print(f"  debug_logger._errors:             {len(debug_logger._errors)}")
    print(f"  debug_logger._warnings:           {len(debug_logger._warnings)}")
    print(f"  debug_logger._info:               {len(debug_logger._info)}")
    print(f"  debug_logger._seen_errors:        {len(debug_logger._seen_errors)}")
    print(f"  debug_logger._stats keys:         {len(debug_logger._stats)}")

    # dynamic_hook_system
    print(f"  dynamic_hook_system.hooks:         {len(dynamic_hook_system.hooks)}")
    print(f"  dynamic_hook_system.instance_hooks: {len(dynamic_hook_system.instance_hooks)}")


async def stress_test(num_instances: int = 3, num_rounds: int = 2):
    """Run the stress test."""
    manager = BrowserManager()
    interceptor = NetworkInterceptor()
    all_snapshots = []

    s = snapshot("BASELINE (before any browsers)")
    print_snapshot(s)
    all_snapshots.append(s)
    print_internal_state()

    for round_num in range(1, num_rounds + 1):
        print(f"\n\n{'#'*60}")
        print(f"  ROUND {round_num}/{num_rounds}")
        print(f"{'#'*60}")

        instance_ids = []

        # --- SPAWN PHASE ---
        for i in range(num_instances):
            label = f"R{round_num} spawn instance {i+1}/{num_instances}"
            print(f"\n>> {label}...")
            try:
                options = BrowserOptions(
                    headless=True,
                    viewport_width=1280,
                    viewport_height=720,
                    sandbox=False,
                )
                instance = await manager.spawn_browser(options)
                instance_ids.append(instance.instance_id)
                print(f"   Spawned: {instance.instance_id}")

                # Setup network interception (this is where handlers accumulate)
                tab = await manager.get_tab(instance.instance_id)
                if tab:
                    await interceptor.setup_interception(tab, instance.instance_id)
            except Exception as e:
                print(f"   FAILED: {e}")

        s = snapshot(f"R{round_num} AFTER SPAWNING {len(instance_ids)} instances")
        print_snapshot(s)
        all_snapshots.append(s)

        # --- NAVIGATE PHASE (generates network traffic) ---
        for iid in instance_ids:
            try:
                await manager.navigate(iid, HEAVY_PAGE, timeout=30000)
                print(f"   Navigated {iid[:8]}... to {HEAVY_PAGE}")
            except Exception as e:
                print(f"   Nav failed {iid[:8]}...: {e}")

        # Let network events accumulate
        await asyncio.sleep(3)

        s = snapshot(f"R{round_num} AFTER NAVIGATION (traffic accumulated)")
        print_snapshot(s)
        all_snapshots.append(s)

        # Check network data size
        for iid in instance_ids:
            reqs = await interceptor.list_requests(iid)
            print(f"   Instance {iid[:8]}... captured {len(reqs)} network requests")

        # --- CLOSE PHASE ---
        for iid in instance_ids:
            try:
                # Simulate what the server's close_instance does
                await manager.close_instance(iid)
                await interceptor.clear_instance_data(iid)
                dynamic_hook_system.remove_instance(iid)
                print(f"   Closed {iid[:8]}...")
            except Exception as e:
                print(f"   Close failed {iid[:8]}...: {e}")

        # Give OS time to reclaim
        await asyncio.sleep(2)
        gc.collect()

        s = snapshot(f"R{round_num} AFTER CLOSING ALL instances")
        print_snapshot(s)
        all_snapshots.append(s)
        print_internal_state()

    # --- FINAL ANALYSIS ---
    print(f"\n\n{'='*60}")
    print(f"  LEAK ANALYSIS SUMMARY")
    print(f"{'='*60}")

    baseline = all_snapshots[0]
    final = all_snapshots[-1]

    python_delta = final["python_rss_mb"] - baseline["python_rss_mb"]
    child_delta = final["child_count"]  # should be 0

    print(f"\n  Python RSS growth:    {python_delta:>+8.1f} MB  (baseline: {baseline['python_rss_mb']:.1f} MB)")
    print(f"  Orphan child procs:   {child_delta:>8d}    (should be 0)")

    # Check for leaked internal state
    print(f"\n  --- Leaked Internal State ---")
    leaked_instances = len(persistent_storage._data.get("instances", {}))
    leaked_prog = len(persistent_storage.get("progressive_elements", {}))
    leaked_hooks = len(dynamic_hook_system.instance_hooks)
    leaked_errors = len(debug_logger._errors)
    leaked_warnings = len(debug_logger._warnings)
    leaked_info = len(debug_logger._info)
    leaked_seen = len(debug_logger._seen_errors)

    print(f"  persistent_storage instances:     {leaked_instances}  {'LEAK!' if leaked_instances > 0 else 'OK'}")
    print(f"  progressive_elements:             {leaked_prog}  {'LEAK!' if leaked_prog > 0 else 'OK'}")
    print(f"  dynamic_hook_system.instance_hooks: {leaked_hooks}  {'LEAK!' if leaked_hooks > 0 else 'OK'}")
    print(f"  debug_logger._errors:             {leaked_errors}  {'grows unboundedly' if leaked_errors > 0 else 'OK'}")
    print(f"  debug_logger._warnings:           {leaked_warnings}  {'grows unboundedly' if leaked_warnings > 0 else 'OK'}")
    print(f"  debug_logger._info:               {leaked_info}  {'grows unboundedly' if leaked_info > 0 else 'OK'}")
    print(f"  debug_logger._seen_errors:        {leaked_seen}  {'grows unboundedly' if leaked_seen > 0 else 'OK'}")

    # Check network interceptor filters leak
    filters_leaked = len(interceptor._instance_filters)
    print(f"  network_interceptor._instance_filters: {filters_leaked}  {'LEAK!' if filters_leaked > 0 else 'OK'}")

    # Memory growth per round
    print(f"\n  --- Memory Growth Timeline ---")
    for s in all_snapshots:
        delta = s["python_rss_mb"] - baseline["python_rss_mb"]
        print(f"  {s['label']:<50s}  {s['python_rss_mb']:>7.1f} MB  ({delta:>+6.1f} MB)  children={s['child_count']}")

    if python_delta > 20:
        print(f"\n  *** WARNING: Significant Python RSS growth ({python_delta:.1f} MB) ***")
    if child_delta > 0:
        print(f"\n  *** WARNING: {child_delta} orphan Chrome processes still running! ***")


def main():
    parser = argparse.ArgumentParser(description="Memory leak stress test")
    parser.add_argument("--instances", type=int, default=3, help="Instances per round")
    parser.add_argument("--rounds", type=int, default=2, help="Number of spawn/close rounds")
    args = parser.parse_args()

    print(f"Memory Leak Stress Test")
    print(f"  Instances per round: {args.instances}")
    print(f"  Rounds: {args.rounds}")
    print(f"  Python PID: {os.getpid()}")

    asyncio.run(stress_test(num_instances=args.instances, num_rounds=args.rounds))


if __name__ == "__main__":
    main()
