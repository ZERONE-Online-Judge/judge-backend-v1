from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models import ProblemAsset
from app.routers import operator
from app.services import testcase_verifier as verifier
from app.services import package_builder
from app.services.package_builder import PackageBuildError, required_package_resources


def asset(name, role="package-resource", seconds=0):
    marker = str(uuid4())
    category = f"package-files/{role}" if role else "assets"
    return ProblemAsset(asset_id=marker, contest_id="contest", problem_id="problem",
        original_filename=name, storage_key=f"contests/contest/problems/problem/{category}/{marker}",
        mime_type="text/plain", file_size=1, sha256="a" * 64,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds))


@pytest.fixture
def context(monkeypatch):
    state = {"assets": [], "sets": [{"testcase_set_id": "active", "is_active": True, "version": 1,
        "testcases": [{"display_order": 1, "input_storage_key": "input", "output_storage_key": "answer"}]}]}
    monkeypatch.setattr(operator.store, "problem_assets_for_problem", lambda *args: state["assets"])
    monkeypatch.setattr(operator.store, "testcase_sets_for_problem", lambda *args: state["sets"])
    monkeypatch.setattr(operator.store, "problem_judge_bundle_status", lambda *args: {"status": "ready"})
    return state


def header_status():
    result = operator._problem_package_status("contest", "problem")
    header = next(item for item in result["support_files"] if item["role"] == "package-resource")
    return result, header


def test_newer_auxiliary_resources_cannot_replace_the_header_status(context):
    header = asset("testlib.h")
    context["assets"] = [header, asset("validator.cpp", "validator"),
        asset("polygon-statement--output.tex", seconds=10), asset("settings.json", seconds=20)]
    result, support = header_status()
    assert result["ready"] is True
    assert support == {"role": "package-resource", "label": "testlib.h", "required": True,
        "count": 1, "latest_filename": "testlib.h", "status": "ready"}
    assert len(context["assets"]) == 4


def test_auxiliary_resources_do_not_satisfy_the_required_header(context):
    context["assets"] = [asset("validator.cpp", "validator"), asset("notes.tex"),
        asset("settings.json"), asset("helpers.h"), asset("testlib.h.bak")]
    result, support = header_status()
    assert result["ready"] is False
    assert support["count"] == 0 and support["latest_filename"] is None and support["status"] == "missing"
    assert "testlib.h 파일이 없습니다." in result["warnings"]


@pytest.mark.parametrize("name", ["nested/testlib.h", "nested\\testlib.h"])
def test_filename_fallback_stays_available_alongside_other_resources(context, name):
    header = asset(name, role=None)
    extra = asset("helpers.h")
    context["assets"] = [extra, header, asset("validator.cpp", "validator")]
    result, support = header_status()
    assert result["ready"] is True and support["count"] == 1
    assert required_package_resources(context["assets"]) == [extra, header]


def test_resource_copy_uses_the_same_portable_basename_as_header_detection(monkeypatch, tmp_path):
    header, custom = asset("nested\\testlib.h"), asset("nested\\helpers.h")
    monkeypatch.setattr(package_builder.object_storage, "read_text", lambda key: "// fixture\n")
    package_builder._copy_package_resources(tmp_path, required_package_resources([header, custom]))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["helpers.h", "testlib.h"]


def test_resource_selection_keeps_custom_files_and_excludes_private_editor_metadata():
    header, helper, config = asset("testlib.h"), asset("helpers.h"), asset("settings.json")
    snapshot = asset("editor-project.json")
    blob = asset("blob-" + "b" * 64 + ".bin")
    assert required_package_resources([header, helper, config, snapshot, blob]) == [header, helper, config]


@pytest.mark.parametrize("operation", ["import", "candidate"])
def test_testcase_verification_rejects_missing_header_before_any_compilation(context, monkeypatch, operation):
    context["assets"] = [asset("validator.cpp", "validator"), asset("checker.cpp", "checker"),
        asset("notes.tex"), asset("settings.json")]
    def forbidden(*args, **kwargs):
        pytest.fail("Missing required headers must be rejected before executing or compiling any program")
    monkeypatch.setattr(verifier, "_prepare_program", forbidden)
    with pytest.raises(PackageBuildError, match=r"testlib\.h package-resource file is required"):
        if operation == "import":
            verifier.build_verified_testcase_set("contest", "problem", [verifier.UploadedTestcase(1, "input", "answer")])
        else:
            verifier.verify_active_testcases_with_candidate_asset("contest", "problem", asset("new-checker.cpp", "checker", 20))


@pytest.mark.parametrize("operation", ["import", "candidate"])
def test_verification_keeps_every_custom_compile_resource_with_the_actual_header(context, monkeypatch, operation):
    header, custom, notes = asset("testlib.h"), asset("helpers.h"), asset("notes.tex", seconds=30)
    context["assets"] = [header, custom, notes, asset("validator.cpp", "validator"), asset("checker.cpp", "checker")]
    captured = []
    class BeforeExecution(Exception):
        pass
    def inspect(work_dir, program, resources):
        captured.extend(resources)
        raise BeforeExecution()
    monkeypatch.setattr(verifier, "_prepare_program", inspect)
    with pytest.raises(BeforeExecution):
        if operation == "import":
            verifier.build_verified_testcase_set("contest", "problem", [verifier.UploadedTestcase(1, "input", "answer")])
        else:
            verifier.verify_active_testcases_with_candidate_asset("contest", "problem", asset("new-checker.cpp", "checker", 20))
    assert captured == [header, custom, notes]
