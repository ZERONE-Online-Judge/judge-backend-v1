"""Bounded task-control tools; only public plans and evidence, not hidden reasoning."""

from app.services import verification_ai as ai
from app.services import verification_agent_tools as caps


def string(value, maximum, *, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise caps.ToolError(f"텍스트는 {minimum}~{maximum}자로 지정하세요.")
    return value.strip()


def handle(row, context, state, files, name, args):
    if name == "list_verification_runs":
        runs = ai.list_runs(row.contest_id, row.problem_id)["runs"]
        runs.sort(key=lambda r: r["submission"]["submitted_at"], reverse=True)
        return {
            "runs": [
                {
                    "submission_id": r["submission"]["submission_id"],
                    "asset_id": r["asset_id"],
                    "filename": r["asset"]["original_filename"],
                    "expected": r["expected_status"],
                    "actual": r["submission"]["status"],
                    "failed_testcase_order": r["submission"]["failed_testcase_order"],
                    "stale": r["stale"],
                }
                for r in runs[:20]
            ]
        }
    if name == "read_verification_run":
        with ai.SessionLocal() as db:
            run, submission = ai._find_run(
                db, row.contest_id, row.problem_id, args["submission_id"]
            )
            if len(submission.source_code.encode()) > caps.MAX_SOURCE:
                raise caps.ToolError(
                    "128 KiB보다 큰 검증 코드는 추가 조회 대상에서 제외합니다."
                )
            extra = state.setdefault("extra_files", {})
            prefix = f"run:{submission.submission_id}:"
            if prefix + "source" not in extra and len(extra) >= 8:
                raise caps.ToolError(
                    "채점 기록의 소스·로그 조회는 작업당 최대 4개입니다."
                )
            suffix = {
                "cpp17": "cpp",
                "c99": "c",
                "python313": "py", "python311": "py",
                "java8": "java", "pypy3": "py", "kotlin": "kt",
            }.get(submission.language, "txt")
            extra[prefix + "source"] = {
                "category": "reference",
                "name": f"{submission.submission_id}.{suffix}",
                "text": submission.source_code,
            }
            extra[prefix + "diagnostics"] = {
                "category": "document",
                "name": "실제 채점 로그",
                "text": caps.clean_log(submission.compile_message, 12000)
                + "\n"
                + caps.clean_log(submission.judge_message, 12000),
            }
            return {
                "file_ids": [prefix + "source", prefix + "diagnostics"],
                "status": submission.status,
                "failed_testcase_order": submission.failed_testcase_order,
                "runtime_ms": submission.runtime_ms,
                "memory_kb": submission.memory_kb,
                "same_context": run.context_hash == row.context_hash,
            }
    if name == "update_plan":
        steps = args["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise caps.ToolError("계획은 1~8개 단계로 작성하세요.")
        plan = []
        for step in steps:
            if step["status"] not in {"pending", "in_progress", "done"}:
                raise caps.ToolError("올바른 계획 상태를 지정하세요.")
            plan.append({"title": string(step["title"], 200), "status": step["status"]})
        state["plan"] = plan
        state["phase"] = next(
            (s["title"] for s in plan if s["status"] == "in_progress"), "검증 계획 수립"
        )
        return {"plan_saved": True, "steps": len(plan)}
    if name == "record_finding":
        entry = {
            "id": string(args["id"], 40),
            "title": string(args["title"], 160),
            "detail": string(args["detail"], 2000),
            "status": args["status"],
        }
        refs = args["evidence_refs"]
        if entry["status"] not in {"confirmed", "hypothesis", "rejected"}:
            raise caps.ToolError("올바른 근거 상태를 지정하세요.")
        if (
            not isinstance(refs, list)
            or len(refs) > 8
            or any(not isinstance(r, str) for r in refs)
        ):
            raise caps.ToolError("근거 ID는 최대 8개입니다.")
        with ai.SessionLocal() as db:
            results = caps.results(db, row.analysis_id)
        known = set(files)
        known.update("workspace:" + p for p in state.get("workspace", {}))
        known.update(
            "trial:" + r["submission_id"]
            for r in results
            if r["status"] not in ai.PENDING
        )
        known.update(
            "experiment:" + r["request_id"] for r in state.get("playground_runs", [])
        )
        known.update(
            "probe-check:" + r["check_id"] for r in state.get("probe_checks", [])
        )
        if any(r not in known for r in refs) or (
            entry["status"] == "confirmed" and not refs
        ):
            raise caps.ToolError(
                "확인한 실제 파일 또는 완료된 실행 ID를 근거로 지정하세요."
            )
        entry["evidence_refs"] = list(dict.fromkeys(refs))
        findings = state.setdefault("findings", [])
        index = next(
            (i for i, old in enumerate(findings) if old["id"] == entry["id"]), None
        )
        if index is None:
            if len(findings) >= 16:
                raise caps.ToolError(
                    "근거는 최대 16개입니다. 기존 항목을 갱신해 정리하세요."
                )
            findings.append(entry)
        else:
            findings[index] = entry
        return {"finding_saved": entry["id"], "status": entry["status"]}
    if name == "ask_user":
        state["question"] = {
            "question": string(args["question"], 1200),
            "reason": string(args["reason"], 1200),
        }
        return {"awaiting_user": True}
    if name == "finish_task":
        from app.services.verification_agent import final_report
        from app.services import verification_candidates as candidates
        from app.services import verification_investigation as investigation

        outcome = "inconclusive" if state.get("finalizing") else args["outcome"]
        if outcome not in {"completed", "inconclusive"}:
            raise caps.ToolError("작업 결과 상태를 지정하세요.")
        if outcome == "completed" and (
            not state.get("plan")
            or any(step["status"] != "done" for step in state["plan"])
        ):
            raise caps.ToolError(
                "마지막 작업 계획을 update_plan으로 실제 수행 결과에 맞게 정리하세요. 끝난 단계만 done으로 표시하고, 미완료 작업이 남았다면 inconclusive로 보고하세요."
            )
        with ai.SessionLocal() as db:
            runs = caps.results(db, row.analysis_id)
        investigation.validate_finish(row, state, args["report"], runs)
        repairs = [
            a for a in state["artifacts"].values() if a.get("purpose") != "comparison"
        ]
        if repairs and outcome == "completed":
            latest = candidates.assessment(row, repairs[-1], runs)
            if latest["status"] in {"pending", "unverified"}:
                raise caps.ToolError(
                    "최종 솔루션 후보를 전체 등록 테스트에서 확인하거나, 미검증 이유를 적고 inconclusive로 마무리하세요."
                )
            if latest["status"] != "passed":
                outcome = "inconclusive"
        report = investigation.normalize(args["report"])
        if report.get("conclusion") == "inconclusive":
            outcome = "inconclusive"
        if not runs and not state.get("playground_runs"):
            report["limitations"].append(
                "이 작업은 자료 검토만 수행했습니다. 실제 코드 실행으로 확인한 결과는 없습니다."
            )
        state["report"] = final_report(row, state, report)
        if state["report"].get("conclusion") == "inconclusive":
            outcome = "inconclusive"
        state["outcome"] = outcome
        state["phase"] = (
            "요청 검토 완료" if outcome == "completed" else "추가 검증 필요"
        )
        return {"report_saved": True, "outcome": outcome}
    raise caps.ToolError("지원하지 않는 작업 도구입니다.")
