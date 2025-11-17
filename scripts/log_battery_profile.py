#!/usr/bin/env python3
"""
Log UPS battery telemetry snapshots for offline analysis.

This utility samples the Geekworm X1201 monitor at a fixed interval, writes the
raw register + derived percentage + voltage to a CSV, and lets the operator tag
events (e.g., “charger unplugged”) by typing notes while the logger is running.
"""

from __future__ import annotations

import argparse
import csv
import select
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parents[1]))

from backend.eink.power import X1201PowerMonitor  # noqa: E402


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_output_path() -> Path:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"battery_profile_{timestamp}.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output_path(),
        help="CSV file to write (default: logs/battery_profile_<timestamp>.csv)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=10.0,
        help="Sampling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "-n",
        "--samples",
        type=int,
        default=0,
        help="Number of samples to record (0 = run until Ctrl-C)",
    )
    return parser.parse_args()


def format_timestamp(ts: Optional[datetime]) -> str:
    if not ts:
        return ""
    return ts.isoformat()


def main() -> int:
    args = parse_args()
    monitor = X1201PowerMonitor(logger=None)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "iteration",
                "voltage_v",
                "raw_capacity_register",
                "capacity_swapped",
                "reported_percentage",
                "voltage_estimate",
                "ac_state",
                "derived_state",
                "low_battery",
                "user_marker",
            ]
        )
        print(
            "Logging UPS telemetry to", args.output,
            "\nType notes (e.g. 'AC unplugged') and press Enter to tag events.",
            "\nPress Ctrl-C to stop.\n",
        )
        iteration = 0
        pending_marker = ""
        try:
            while True:
                start = time.time()
                status = monitor.read_status()
                raw_capacity = monitor._read_register(monitor.CAPACITY_REGISTER)  # pylint: disable=protected-access
                swapped = None
                if raw_capacity is not None:
                    swapped = struct.unpack("<H", struct.pack(">H", raw_capacity))[0]
                voltage_est = monitor._estimate_percentage_from_voltage(  # pylint: disable=protected-access
                    status.voltage if status else None
                )
                writer.writerow(
                    [
                        format_timestamp(utcnow()),
                        iteration,
                        status.voltage if status else "",
                        raw_capacity if raw_capacity is not None else "",
                        swapped if swapped is not None else "",
                        status.percentage if status else "",
                        round(voltage_est, 2) if voltage_est is not None else "",
                        status.ac_power if status else "",
                        status.state if status else "",
                        status.low_battery if status else "",
                        pending_marker,
                    ]
                )
                handle.flush()
                summary = (
                    f"[{iteration}] "
                    f"{status.voltage:.3f}V " if status and status.voltage else f"[{iteration}] "
                )
                if status and status.percentage is not None:
                    summary += f"{status.percentage:.1f}% "
                if voltage_est is not None:
                    summary += f"(volt≈{voltage_est:.1f}%) "
                if pending_marker:
                    summary += f"marker='{pending_marker}'"
                print(summary.strip())
                pending_marker = ""
                iteration += 1
                if args.samples and iteration >= args.samples:
                    break
                elapsed = time.time() - start
                timeout = max(0.0, args.interval - elapsed)
                if timeout:
                    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
                    if rlist:
                        note = sys.stdin.readline().strip()
                        if note:
                            pending_marker = note
                            print(f"Tagged event: {note}")
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
