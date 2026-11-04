from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import tempfile
import re

from app.models import ProblemAsset
from app.services.package_builder import PackageBuildError, _prepare_program, _run_checker, _run_program, package_role, required_package_resources
from app.services.storage import object_storage
from app.services.store import store

_EXECUTABLE_SOURCE_SUFFIXES = {".cpp", ".c", ".py", ".java"}


@dataclass
class UploadedTestcase:
    display_order: int
    input_storage_key: str
    output_storage_key: str
    input_sha256: str | None = None
    output_sha256: str | None = None


def build_verified_testcase_set(contest_id: str, problem_id: str, cases: list[UploadedTestcase]) -> dict:
    if not cases:
        raise PackageBuildError("At least one .in/.out testcase pair is required.")

    assets = store.problem_assets_for_problem(contest_id, problem_id)
    role_assets: dict[str, list[ProblemAsset]] = {}
    for asset in assets:
        role = package_role(asset)
        if role:
            role_assets.setdefault(role, []).append(asset)

    validator_asset = _latest_required(role_assets, "validator", assets)
    checker_asset = _latest_required(role_assets, "checker", assets)
    package_resources = required_package_resources(assets)

    validated: list[tuple[UploadedTestcase, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="zoj-testcase-verify-") as temp:
        work_root = Path(temp)
        validator = _prepare_program(work_root / "validator", validator_asset, package_resources)
        checker = _prepare_program(work_root / "checker", checker_asset, package_resources)
        for case in sorted(cases, key=lambda item: item.display_order):
            input_bytes = object_storage.read_bytes(case.input_storage_key)
            output_bytes = object_storage.read_bytes(case.output_storage_key)
            _check_sha(case.input_sha256, input_bytes, f"input test {case.display_order}")
            _check_sha(case.output_sha256, output_bytes, f"output test {case.display_order}")
            input_text = _normalize_text_for_tooling(input_bytes)
            output_text = _normalize_text_for_tooling(output_bytes)
            _run_program(validator, args=[], stdin=input_text, label=f"validator test {case.display_order}", capture_stdout=False)
            _run_checker(checker, work_root, case.display_order, input_text, output_text)
            validated.append((case, input_text, output_text))

    sets = store.testcase_sets_for_problem(contest_id, problem_id)
    active_set = next((item for item in sets if item.get("is_active")), None)
    testcase_set = store.create_testcase_set(contest_id, problem_id, is_active=True) if not active_set else store.update_testcase_set(
        contest_id, problem_id, active_set["testcase_set_id"], is_active=True
    )
    if not testcase_set:
        raise PackageBuildError("Active testcase set is not available.")

    merged_by_stem: dict[str, dict] = {}
    if active_set:
        for row in active_set.get("testcases", []):
            stem = _testcase_stem(row.get("input_storage_key") or row.get("output_storage_key") or "")
            if not stem:
                continue
            merged_by_stem[stem] = {
                "input_storage_key": row["input_storage_key"],
                "output_storage_key": row["output_storage_key"],
                "input_sha256": row["input_sha256"],
                "output_sha256": row["output_sha256"],
            }

    for case, input_text, output_text in validated:
        stem = _testcase_stem(case.input_storage_key) or _testcase_stem(case.output_storage_key)
        if not stem:
            stem = f"{case.display_order:03d}"
        merged_by_stem[stem] = {
            "input_storage_key": case.input_storage_key,
            "output_storage_key": case.output_storage_key,
            "input_sha256": case.input_sha256 or hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
            "output_sha256": case.output_sha256 or hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        }

    merged_cases_payload = []
    for index, stem in enumerate(sorted(merged_by_stem.keys()), start=1):
        item = merged_by_stem[stem]
        merged_cases_payload.append(
            {
                "display_order": index,
                "input_storage_key": item["input_storage_key"],
                "output_storage_key": item["output_storage_key"],
                "input_sha256": item["input_sha256"],
                "output_sha256": item["output_sha256"],
                "time_limit_ms_override": None,
                "memory_limit_mb_override": None,
            }
        )

    replaced_cases = store.replace_testcases_in_set(contest_id, problem_id, testcase_set.testcase_set_id, merged_cases_payload)
    created_cases = [item.model_dump(mode="json") for item in replaced_cases]

    return {
        "testcase_set": testcase_set.model_dump(mode="json"),
        "testcases": created_cases,
        "verified_count": len(created_cases),
        "checks": {
            "validator": True,
            "checker": True,
            "package_resource_count": len(package_resources),
        },
    }


def verify_active_testcases_with_candidate_asset(contest_id: str, problem_id: str, candidate_asset: ProblemAsset) -> dict | None:
    candidate_role = package_role(candidate_asset)
    if candidate_role not in {"validator", "checker", "package-resource"}:
        return None

    sets = store.testcase_sets_for_problem(contest_id, problem_id)
    active_set = next((item for item in sets if item.get("is_active")), None)
    active_cases = active_set.get("testcases", []) if active_set else []
    if not active_set or not active_cases:
        return {
            "candidate_role": candidate_role,
            "checked": False,
            "reason": "active testcase set is empty",
        }

    assets = [*store.problem_assets_for_problem(contest_id, problem_id), candidate_asset]
    role_assets: dict[str, list[ProblemAsset]] = {}
    for asset in assets:
        role = package_role(asset)
        if role:
            role_assets.setdefault(role, []).append(asset)

    package_resources = required_package_resources(assets)

    should_run_validator = candidate_role in {"validator", "package-resource"}
    should_run_checker = candidate_role in {"checker", "package-resource"}

    with tempfile.TemporaryDirectory(prefix="zoj-asset-verify-") as temp:
        work_root = Path(temp)
        validator = _prepare_program(work_root / "validator", _latest_required(role_assets, "validator", assets), package_resources) if should_run_validator else None
        checker = _prepare_program(work_root / "checker", _latest_required(role_assets, "checker", assets), package_resources) if should_run_checker else None
        for index, case in enumerate(sorted(active_cases, key=lambda item: item.get("display_order", 0)), start=1):
            display_order = int(case.get("display_order") or index)
            input_text = _normalize_text_for_tooling(object_storage.read_bytes(case["input_storage_key"]))
            output_text = _normalize_text_for_tooling(object_storage.read_bytes(case["output_storage_key"]))
            if validator:
                _run_program(validator, args=[], stdin=input_text, label=f"validator test {display_order}", capture_stdout=False)
            if checker:
                _run_checker(checker, work_root, display_order, input_text, output_text)

    return {
        "candidate_role": candidate_role,
        "checked": True,
        "testcase_set_id": active_set["testcase_set_id"],
        "verified_count": len(active_cases),
        "validator": should_run_validator,
        "checker": should_run_checker,
    }


def _latest_required(role_assets: dict[str, list[ProblemAsset]], role: str, all_assets: list[ProblemAsset]) -> ProblemAsset:
    items = role_assets.get(role, [])
    if not items:
        fallback_name = {
            "validator": "validator.cpp",
            "checker": "checker.cpp",
        }.get(role)
        if fallback_name:
            fallback_items = [asset for asset in all_assets if Path(asset.original_filename).name.lower() == fallback_name]
            if fallback_items:
                items = fallback_items
    if not items:
        raise PackageBuildError(f"{role} file is required.")
    executable_items = [item for item in items if Path(item.original_filename).suffix.lower() in _EXECUTABLE_SOURCE_SUFFIXES]
    if not executable_items:
        names = ", ".join(sorted({item.original_filename for item in items})[:5])
        raise PackageBuildError(
            f"{role} executable source is required (.cpp/.c/.py/.java). "
            f"Current {role} files: {names or 'none'}"
        )
    return sorted(executable_items, key=lambda item: item.created_at)[-1]


def _check_sha(expected: str | None, content: bytes, label: str) -> None:
    if not expected:
        return
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise PackageBuildError(f"{label} sha256 mismatch.")


def _normalize_text_for_tooling(content: bytes) -> str:
    text = content.decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _testcase_stem(storage_key: str) -> str:
    name = Path(storage_key).name
    stem = re.sub(r"\.(in|out)$", "", name, flags=re.IGNORECASE)
    stem = re.sub(r"^\d{10,16}-", "", stem)
    return stem
