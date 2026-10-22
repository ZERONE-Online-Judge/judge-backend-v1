import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from app.services import verification_workspace as workspace
from app.services.verification_agent_tools import ToolError
from app.settings import settings
from playground import service


def state():
    return {"workspace": {}, "artifacts": {}}


def invoke(s, name, **args):
    return workspace.handle(
        SimpleNamespace(analysis_id="test-analysis"),
        {},
        s,
        {"original": {"text": "print(4)"}},
        name,
        args,
        "tool-call",
    )


def test_workspace_files_are_scoped_and_editable_without_sending_whole_inputs():
    s = state()
    invoke(s, "workspace_copy", file_id="original", path="src/main.py")
    invoke(s, "workspace_write", path="case.in", content="2 3\n")
    invoke(s, "workspace_patch", path="src/main.py", old="print(4)", new="print(5)")
    assert (
        invoke(s, "workspace_read", path="src/main.py", offset=0, length=120)["content"]
        == "print(5)"
    )
    assert invoke(s, "workspace_list")["files"][0]["path"] == "case.in"
    assert "content" not in invoke(s, "workspace_list")["files"][0]
    candidate = invoke(
        s, "workspace_candidate", path="src/main.py", language="python311"
    )
    assert s["artifacts"][candidate["artifact_id"]]["source"] == "print(5)"
    invoke(s, "workspace_delete", path="case.in")
    assert "case.in" not in s["workspace"]
    assert state()["workspace"] == {}


@pytest.mark.parametrize(
    "path", ["../secrets", "/etc/passwd", "foo//bar", "foo/../bar", "", "x\x00y"]
)
def test_workspace_rejects_path_escape(path):
    with pytest.raises(ToolError):
        workspace.put(state(), path, b"x")
    with pytest.raises(ValueError):
        service.validate_files({path: base64.b64encode(b"x").decode()})


def test_workspace_has_hard_storage_limits():
    with pytest.raises(ToolError):
        workspace.put(state(), "big", b"x" * (256 * 1024 + 1))
    s = state()
    for i in range(48):
        workspace.put(s, str(i), b"x")
    with pytest.raises(ToolError):
        workspace.put(s, "one-too-many", b"x")


def test_execute_uses_only_private_service_and_preserves_files(monkeypatch):
    monkeypatch.setattr(
        settings, "verification_playground_url", "http://private-playground:8090"
    )
    monkeypatch.setattr(
        settings, "verification_playground_token", SecretStr("test-private-token")
    )
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        assert "OPENAI" not in json.dumps(kwargs["json"])
        return httpx.Response(
            200,
            json={
                "runtime": "gvisor",
                "network": "disabled",
                "files": {"answer.txt": base64.b64encode(b"5").decode()},
                "stdout": "5\n",
                "exit_code": 0,
                "notes": [],
            },
        )

    monkeypatch.setattr(workspace.httpx, "post", post)
    s = state()
    workspace.put(s, "initial.py", b"print(5)")
    result = invoke(
        s, "workspace_exec", command="python initial.py > answer.txt", timeout_seconds=5
    )
    assert result["exit_code"] == 0
    assert list(s["workspace"]) == ["answer.txt"]
    assert requests[0][0] == "http://private-playground:8090/execute"
    assert requests[0][1]["follow_redirects"] is False


def test_execute_lost_response_retries_same_id_and_has_finite_attempt_budget(
    monkeypatch,
):
    monkeypatch.setattr(
        settings, "verification_playground_url", "http://private-playground:8090"
    )
    monkeypatch.setattr(
        settings, "verification_playground_token", SecretStr("test-private-token")
    )
    ids = []

    def timeout(url, **kwargs):
        ids.append(kwargs["json"]["request_id"])
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(workspace.httpx, "post", timeout)
    s = state()
    assert invoke(s, "workspace_exec", command="true", timeout_seconds=1) is None
    assert invoke(s, "workspace_exec", command="true", timeout_seconds=1) is None
    with pytest.raises(ToolError):
        invoke(s, "workspace_exec", command="true", timeout_seconds=1)
    assert len(set(ids)) == 1 and len(s["playground_attempt_ids"]) == 1
    s["playground_attempt_ids"] = ["fake" + str(i) for i in range(12)]
    with pytest.raises(ToolError, match="12회"):
        invoke(s, "workspace_exec", command="another command", timeout_seconds=1)


def test_runner_cannot_fall_back_to_unisolated_docker():
    client = Mock()
    client.get.return_value.json.return_value = {"Runtimes": {"runc": {}}}
    with pytest.raises(ValueError, match="gVisor"):
        service.ready(client)
    spec = service.create_spec()
    assert spec["HostConfig"]["Runtime"] == "runsc"
    assert spec["HostConfig"]["NetworkMode"] == "none"
    assert not spec["HostConfig"].get("Binds") and not spec["HostConfig"].get(
        "Privileged"
    )
    assert "docker.sock" not in json.dumps(spec)
    assert "OPENAI" not in json.dumps(spec) and "TOKEN" not in json.dumps(spec)
    assert spec["HostConfig"]["Memory"] == 768 * 1024 * 1024


def test_registered_judge_files_are_readonly_in_tools_and_execution_results(
    monkeypatch,
):
    s = state()
    files = {"judge": {"text": "// registered checker", "read_only": True}}

    def tool(name, **args):
        return workspace.handle(
            SimpleNamespace(analysis_id="protected"), {}, s, files, name, args, name
        )

    tool("workspace_copy", file_id="judge", path="judge/checker.cpp")
    assert workspace.manifest(s)[0]["read_only"]
    for name, args in [
        ("workspace_write", {"content": "fake"}),
        ("workspace_delete", {}),
        ("workspace_patch", {"old": "registered", "new": "fake"}),
        ("workspace_candidate", {"language": "cpp17"}),
    ]:
        with pytest.raises(ToolError):
            tool(name, path="judge/checker.cpp", **args)
    monkeypatch.setattr(settings, "verification_playground_url", "http://private:8090")
    monkeypatch.setattr(
        settings, "verification_playground_token", SecretStr("test-token")
    )

    def tampered(url, **kwargs):
        assert kwargs["json"]["readonly"] == ["judge/checker.cpp"]
        return httpx.Response(
            200,
            json={
                "runtime": "gvisor",
                "network": "disabled",
                "readonly_enforced": True,
                "files": {"judge/checker.cpp": base64.b64encode(b"tampered").decode()},
            },
        )

    monkeypatch.setattr(workspace.httpx, "post", tampered)
    with pytest.raises(ToolError, match="보호"):
        tool("workspace_exec", command="true", timeout_seconds=1)
    assert (
        base64.b64decode(s["workspace"]["judge/checker.cpp"])
        == b"// registered checker"
    )


def test_controller_parallelism_is_bounded_and_duplicate_ids_never_execute(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(service, "SLOTS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(service, "INFLIGHT", set())
    entered, release = threading.Barrier(3), threading.Event()
    seen = []

    def execute(job):
        seen.append(job["request_id"])
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return job["request_id"]

    monkeypatch.setattr(service, "_execute", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.execute, {"request_id": "first"})
        second = pool.submit(service.execute, {"request_id": "second"})
        try:
            entered.wait(timeout=5)
            with pytest.raises(service.BusyError):
                service.execute({"request_id": "first"})
            with pytest.raises(service.BusyError):
                service.execute({"request_id": "third"})
        finally:
            release.set()
        assert first.result() == "first" and second.result() == "second"
    assert set(seen) == {"first", "second"} and not service.INFLIGHT
