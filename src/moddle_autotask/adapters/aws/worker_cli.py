"""CLI for the approved-work AWS lab worker."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Never

from moddle_autotask.adapters.moodle.approval_state import ApprovalState

from .labs import AwsEc2LabProvider, AwsLabConfig
from .worker import process_one


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(prog="moodle-autotask-worker", allow_abbrev=False)
    parser.add_argument("run", nargs="?")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--provisioner-role-arn", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument("--instance-profile-name", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--instance-type", default="t3.large")
    parser.add_argument("--root-volume-size-gib", type=int, default=80)
    parser.add_argument("--environment", default="development")
    parser.add_argument("--interval-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    if args.run != "run" or not 5 <= args.interval_seconds <= 3600:
        parser.error("run command and valid interval are required")
    try:
        state = ApprovalState(args.state)
        provider = AwsEc2LabProvider(
            AwsLabConfig(
                region=args.region,
                provisioner_role_arn=args.provisioner_role_arn,
                subnet_id=args.subnet_id,
                security_group_id=args.security_group_id,
                instance_profile_name=args.instance_profile_name,
                image_id=args.image_id,
                instance_type=args.instance_type,
                root_volume_size_gib=args.root_volume_size_gib,
                environment=args.environment,
            )
        )
        owner = f"{socket.gethostname()}:{os.getpid()}"
        while True:
            cycle = process_one(state, provider, owner=owner)
            print(
                json.dumps(
                    {
                        "kind": "approved-work-cycle-v1",
                        "result": cycle.result,
                        "mode": None if cycle.mode is None else cycle.mode.value,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError):
        print("approved-work worker failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
