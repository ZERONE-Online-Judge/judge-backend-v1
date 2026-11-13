"""Administrator-only credential lifecycle; no HTTP enrollment endpoint."""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models import JudgeJobStatus, SubmissionStatus, now_utc
from app.orm_models import JudgeJobRow, JudgeNodeRow, SubmissionRow, EditorJudgeRunRow, EditorExecutionTaskRow
from app.services.security import hash_password
from app.services.store import store


def update_credential(name: str, *, enabled: bool | None = None, secret: str | None = None) -> None:
    with SessionLocal() as db:
        node = db.scalar(select(JudgeNodeRow).where(JudgeNodeRow.node_name == name).with_for_update())
        if node is None:
            raise ValueError("unknown node")
        if enabled is not None:
            node.schedulable = enabled
        if secret is not None:
            node.node_secret_hash = hash_password(secret)
        if enabled is False or secret is not None:
            jobs = db.scalars(select(JudgeJobRow).where(
                JudgeJobRow.assigned_node_id == node.judge_node_id,
                JudgeJobRow.status == JudgeJobStatus.RUNNING.value,
            ).order_by(JudgeJobRow.judge_job_id).with_for_update()).all()
            for job in jobs:
                job.status = JudgeJobStatus.PENDING.value
                job.assigned_node_id = None
                job.lease_token = None
                job.leased_at = None
                from app.services.rejudging import requeue
                if requeue(db, job):
                    continue
                submission = db.get(SubmissionRow, job.submission_id)
                if submission:
                    submission.status = SubmissionStatus.WAITING.value
                    submission.status_updated_at = now_utc()
                    submission.progress_current = None
                    submission.progress_total = None
                    submission.progress_phase = None
            for model, states in [(EditorJudgeRunRow, ["preparing", "judging"]), (EditorExecutionTaskRow, ["running"])]:
                for task in db.scalars(select(model).where(model.assigned_node_id == node.judge_node_id, model.status.in_(states)).with_for_update()):
                    task.status = "waiting"
                    task.assigned_node_id = task.lease_token = task.leased_at = None
                    if isinstance(task, EditorJudgeRunRow):
                        task.progress_current = 0
                        task.progress_phase = None
            node.running_job_count = 0
            node.free_slots = node.total_slots
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "provision", "revoke", "enable", "rotate"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--slots", type=int, default=10)
    parser.add_argument("--secret-stdin", action="store_true", help="Read the secret from stdin instead of a hidden prompt")
    args = parser.parse_args()
    if args.action == "list":
        with SessionLocal() as db:
            for node in db.scalars(select(JudgeNodeRow).order_by(JudgeNodeRow.node_name)):
                print(node.node_name, node.judge_node_id, "enabled" if node.schedulable else "revoked", node.last_heartbeat_at)
        return
    if not args.name:
        parser.error("node name is required")
    secret = None
    if args.action in {"provision", "rotate"}:
        secret = sys.stdin.readline().rstrip("\r\n") if args.secret_stdin else getpass.getpass("Node secret (at least 32 characters): ")
        if not 32 <= len(secret) <= 1024:
            parser.error("secret must be 32 to 1024 characters")
    try:
        if args.action == "provision":
            node = store.provision_node(args.name, secret, args.slots)
            print("Provisioned:", node.node_name, node.judge_node_id)
        else:
            enabled = {"enable": True, "revoke": False}.get(args.action)
            update_credential(args.name, enabled=enabled, secret=secret)
            print(args.action + ":", args.name)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
