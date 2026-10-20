"""Pause or resume the deployed pipeline by hand (the budget kill switch uses the same code).

make pause    # stop everything that costs money
make resume   # undo it
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from canvas_reminder.killswitch import pause, resume  # noqa: E402

STACK = "canvas-reminder-pipeline"
REGION = "ap-southeast-2"


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("pause", "resume"):
        raise SystemExit("usage: pause_resume.py pause|resume")

    stack = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=STACK)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Stacks"][0]["Outputs"]}

    if action == "pause" and input("Stop all reminders? [y/N]: ").lower() != "y":
        return
    run = pause if action == "pause" else resume
    for line in run(
        boto3.client("scheduler", region_name=REGION),
        boto3.client("lambda", region_name=REGION),
        schedule_name=outputs["ScheduleName"],
        functions=[outputs["FunctionName"], outputs["AppleFeedFunctionName"]],
    ):
        print(f"  ✓ {line}")
    print("Paused. Run `make resume` to turn it back on." if action == "pause" else "Resumed.")


if __name__ == "__main__":
    main()
