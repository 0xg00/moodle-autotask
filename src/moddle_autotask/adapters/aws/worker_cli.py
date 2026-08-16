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
from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.submission import MoodleSubmissionClient
from moddle_autotask.adapters.moodle.telegram import TelegramClient, TelegramConfig
from moddle_autotask.health import pulse

from .agent_spool import FileAgentBroker
from .artifacts import AwsMoodleArtifactPreparer
from .completion import TelegramExecutionNotifier
from .image_imports import AwsImageImportConfig, AwsImageImporter
from .input_transfer import AwsGuestInputTransfer
from .labs import AwsCliJsonRunner, AwsEc2LabProvider, AwsLabConfig
from .retention_fs import RetentionFilesystem, RetentionRoots
from .retention_runtime import ControllerRetentionCoordinator, production_ownership
from .worker import process_one


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(prog="moodle-autotask-worker", allow_abbrev=False)
    parser.add_argument("run", nargs="?")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--telegram-config-file", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--artifact-bucket", required=True)
    parser.add_argument("--image-importer-role-arn", required=True)
    parser.add_argument("--vmimport-role-name", required=True)
    parser.add_argument("--provisioner-role-arn", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument("--instance-profile-name", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--instance-type", default="t3.large")
    parser.add_argument("--root-volume-size-gib", type=int, default=80)
    parser.add_argument("--environment", default="development")
    parser.add_argument(
        "--working-directory", type=Path, default=Path("/var/lib/moodle-autotask/artifacts")
    )
    parser.add_argument("--agent-jobs", type=Path, default=Path("/var/spool/moodle-autotask/jobs"))
    parser.add_argument(
        "--agent-results", type=Path, default=Path("/var/spool/moodle-autotask/results")
    )
    parser.add_argument(
        "--retention-controller-private", type=Path, default=Path("/var/lib/moodle-autotask")
    )
    parser.add_argument(
        "--retention-agent-private", type=Path, default=Path("/var/lib/moodle-agent")
    )
    parser.add_argument(
        "--retention-workspaces", type=Path, default=Path("/var/lib/moodle-agent/workspaces")
    )
    parser.add_argument(
        "--retention-bundles",
        type=Path,
        default=Path("/var/spool/moodle-autotask/results/bundles"),
    )
    parser.add_argument("--retention-scratch-ttl", type=int, default=86_400)
    parser.add_argument("--retention-evidence-ttl", type=int, default=604_800)
    parser.add_argument("--retention-candidate-limit", type=int, default=1_024)
    parser.add_argument("--retention-scan-limit", type=int, default=1_024)
    parser.add_argument("--interval-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    if (
        args.run != "run"
        or not 5 <= args.interval_seconds <= 3600
        or not 1 <= args.retention_scratch_ttl <= 90 * 24 * 3600
        or not 1 <= args.retention_evidence_ttl <= 90 * 24 * 3600
        or not 1 <= args.retention_candidate_limit <= 10_000
        or not 1 <= args.retention_scan_limit <= 10_000
    ):
        parser.error("run command and valid interval are required")
    try:
        state = ApprovalState(args.state)
        runner = AwsCliJsonRunner(timeout_seconds=3600)
        lab_runner = AwsCliJsonRunner(timeout_seconds=30)
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
            ),
            lab_runner,
        )
        artifact_preparer = AwsMoodleArtifactPreparer(
            MoodleConnectionConfig.from_token_file(args.token_file),
            args.artifact_bucket,
            args.region,
            args.working_directory,
            runner,
        )
        image_importer = AwsImageImporter(
            AwsImageImportConfig(
                args.region,
                args.image_importer_role_arn,
                args.vmimport_role_name,
                environment=args.environment,
            ),
            runner,
        )
        retention_roots = RetentionRoots(
            controller_private=args.retention_controller_private,
            shared_jobs=args.agent_jobs,
            agent_private=args.retention_agent_private,
            agent_results=args.agent_results,
            agent_workspaces=args.retention_workspaces,
            agent_bundles=args.retention_bundles,
        )
        retention = ControllerRetentionCoordinator(
            state,
            RetentionFilesystem(retention_roots, production_ownership()),
            scratch_ttl=args.retention_scratch_ttl,
            evidence_ttl=args.retention_evidence_ttl,
            candidate_limit=args.retention_candidate_limit,
            scan_limit=args.retention_scan_limit,
        )
        execution_broker = FileAgentBroker(
            args.agent_jobs,
            args.agent_results,
            args.region,
            runner,
            controller_retention_root=args.agent_jobs / ".retention",
        )
        guest_input_transfer = AwsGuestInputTransfer(runner, args.region)
        telegram_config = TelegramConfig.from_file(args.telegram_config_file)
        execution_notifier = TelegramExecutionNotifier(
            telegram_config, TelegramClient(telegram_config), args.agent_results / "bundles"
        )
        submission_service = MoodleSubmissionClient(
            MoodleConnectionConfig.from_token_file(args.token_file)
        )
        owner = f"{socket.gethostname()}:{os.getpid()}"
        while True:
            pulse("worker")
            retention_result = retention.cycle()
            cycle = None
            if retention_result == "idle":
                cycle = process_one(
                    state,
                    provider,
                    owner=owner,
                    artifact_preparer=artifact_preparer,
                    image_importer=image_importer,
                    execution_broker=execution_broker,
                    execution_notifier=execution_notifier,
                    guest_input_transfer=guest_input_transfer,
                    submission_service=submission_service,
                    lease_seconds=3600,
                )
            print(
                json.dumps(
                    {
                        "kind": "approved-work-cycle-v1",
                        "result": retention_result if cycle is None else cycle.result,
                        "mode": None if cycle is None or cycle.mode is None else cycle.mode.value,
                        "retention": retention_result,
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
