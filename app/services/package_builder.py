from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import re

from app.models import ProblemAsset
from app.services.storage import object_storage
from app.settings import settings


@dataclass
class PreparedProgram:
    name: str
    command: list[str]
    work_dir: Path


class PackageBuildError(ValueError):
    pass


PACKAGE_ROLES = {
    "checker",
    "validator",
    "package-resource",
}

_EXECUTABLE_SOURCE_SUFFIXES = {".cpp", ".c", ".py", ".java"}


def is_editor_source_asset(asset: ProblemAsset) -> bool:
    """Reserved Editor archive members contain private source and test metadata."""
    return asset.original_filename == "editor-project.json" or bool(
        re.fullmatch(r"blob-[a-f0-9]{64}\.bin", asset.original_filename)
    )


def package_role(asset: ProblemAsset) -> str | None:
    if is_editor_source_asset(asset):
        return None
    for role in PACKAGE_ROLES:
        if (
            f"/package-files/{role}/" in asset.storage_key
            or f"/support/{role}/" in asset.storage_key
        ):
            return role
    return None


def testlib_header_assets(assets: list[ProblemAsset]) -> list[ProblemAsset]:
    return [asset for asset in assets if Path(asset.original_filename.replace("\\", "/")).name.lower() == "testlib.h"]


def required_package_resources(assets: list[ProblemAsset]) -> list[ProblemAsset]:
    headers = testlib_header_assets(assets)
    if not headers:
        raise PackageBuildError("testlib.h package-resource file is required.")
    resources = [asset for asset in assets if package_role(asset) == "package-resource"]
    known = {asset.asset_id for asset in resources}
    # Keep the existing filename fallback even when custom resources are present.
    resources.extend(asset for asset in headers if asset.asset_id not in known)
    return resources


def _prepare_program(work_dir: Path, asset: ProblemAsset, resources: list[ProblemAsset]) -> PreparedProgram:
    work_dir.mkdir(parents=True, exist_ok=True)
    _copy_package_resources(work_dir, resources)
    source_name = Path(asset.original_filename).name
    suffix = Path(source_name).suffix.lower()
    source = object_storage.read_text(asset.storage_key)
    if suffix == ".py":
        source_path = work_dir / source_name
        source_path.write_text(source, encoding="utf-8")
        return PreparedProgram(source_name, ["python3.13" if shutil.which("python3.13") else "python3", str(source_path)], work_dir)
    if suffix == ".cpp":
        source_path = work_dir / source_name
        source_path.write_text(source, encoding="utf-8")
        binary = work_dir / "main"
        _compile(["g++", "-std=c++17", "-O2", source_path.name, "-o", binary.name], work_dir, source_name)
        return PreparedProgram(source_name, [str(binary)], work_dir)
    if suffix == ".c":
        source_path = work_dir / source_name
        source_path.write_text(source, encoding="utf-8")
        binary = work_dir / "main"
        _compile(["gcc", "-std=c99", "-O2", source_path.name, "-o", binary.name], work_dir, source_name)
        return PreparedProgram(source_name, [str(binary)], work_dir)
    if suffix == ".java":
        source_path = work_dir / "Main.java"
        source_path.write_text(source, encoding="utf-8")
        _compile(["javac", "--release", "8", source_path.name], work_dir, source_name)
        return PreparedProgram(source_name, ["java", "-cp", str(work_dir), "Main"], work_dir)
    raise PackageBuildError(f"Unsupported package source type: {source_name}")


def _copy_package_resources(work_dir: Path, resources: list[ProblemAsset]) -> None:
    for resource in resources:
        resource_name = Path(resource.original_filename.replace("\\", "/")).name
        if not resource_name:
            continue
        target = work_dir / resource_name
        if target.exists():
            continue
        target.write_text(object_storage.read_text(resource.storage_key), encoding="utf-8")


def _compile(command: list[str], cwd: Path, label: str) -> None:
    completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
    if completed.returncode != 0:
        raise PackageBuildError(f"{label} compile failed: {(completed.stderr or completed.stdout)[-4000:]}")


def _run_program(program: PreparedProgram, args: list[str], stdin: str, label: str, capture_stdout: bool = True) -> str:
    completed = subprocess.run(
        [*program.command, *args],
        cwd=program.work_dir,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=settings.package_build_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise PackageBuildError(f"{label} failed: {(completed.stderr or completed.stdout)[-4000:]}")
    return completed.stdout if capture_stdout else ""


def _run_checker(checker: PreparedProgram, work_root: Path, order: int, input_text: str, expected: str) -> None:
    case_dir = work_root / "checker-cases"
    case_dir.mkdir(exist_ok=True)
    input_path = case_dir / f"{order:03d}.in"
    expected_path = case_dir / f"{order:03d}.out"
    participant_path = case_dir / f"{order:03d}.participant.out"
    input_path.write_text(input_text, encoding="utf-8")
    expected_path.write_text(expected, encoding="utf-8")
    participant_path.write_text(expected, encoding="utf-8")
    _run_program(checker, [str(input_path), str(expected_path), str(participant_path)], "", f"checker test {order}", capture_stdout=False)
