"""A per-analysis virtual filesystem and a narrow client to the isolated runner."""

import base64
import hashlib
import json
from pathlib import PurePosixPath

import httpx

from app.services import verification_ai as ai
from app.services import verification_agent_tools as caps
from app.settings import settings

MAX_FILE = 256 * 1024
MAX_TOTAL = 2 * 1024 * 1024
MAX_FILES = 48


def configured():
    return bool(
        settings.verification_playground_url
        and settings.verification_playground_token
        and settings.verification_playground_token.get_secret_value()
    )


def path_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 160
        or PurePosixPath(name).is_absolute()
        or any(p in ("", ".", "..") for p in name.split("/"))
        or "\x00" in name
    ):
        raise caps.ToolError(
            "작업 폴더 안의 상대 파일 경로를 지정하세요. 상위 경로·절대 경로는 허용하지 않습니다."
        )
    return name


def put(state, path, raw):
    path = path_name(path)
    files = state.setdefault("workspace", {})
    if path in state.get("workspace_readonly", {}):
        raise caps.ToolError(
            "채점 기준 파일은 읽기 전용입니다. 원본으로 재현하고 의심 사항을 보고하세요."
        )
    if len(raw) > MAX_FILE:
        raise caps.ToolError("작업 파일은 256 KiB 이하만 지원합니다.")
    encoded = base64.b64encode(raw).decode()
    tentative = {**files, path: encoded}
    if (
        len(tentative) > MAX_FILES
        or sum(len(base64.b64decode(v)) for v in tentative.values()) > MAX_TOTAL
    ):
        raise caps.ToolError(
            "작업 공간의 파일 48개·총 2 MiB 보관 한도입니다. 불필요한 파일을 삭제하세요."
        )
    files[path] = encoded
    return {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def manifest(state):
    return [
        {
            "path": p,
            "read_only": p in state.get("workspace_readonly", {}),
            "bytes": len(base64.b64decode(v)),
            "sha256": hashlib.sha256(base64.b64decode(v)).hexdigest(),
        }
        for p, v in sorted(state.get("workspace", {}).items())
    ]


def handle(row, context, state, files, name, args, call_id):
    workspace = state.setdefault("workspace", {})
    protected = {
        item["sha256"]: file_id
        for file_id, item in files.items()
        if item.get("read_only") and item.get("sha256")
    }
    for path, encoded in workspace.items():
        checksum = hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        if checksum in protected:
            state.setdefault("workspace_readonly", {})[path] = {
                "file_id": protected[checksum],
                "sha256": checksum,
            }
    if name == "workspace_list":
        return {
            "files": manifest(state),
            "max_files": MAX_FILES,
            "max_total_bytes": MAX_TOTAL,
        }
    if name == "workspace_copy":
        raw = caps.file_bytes(files, args["file_id"])
        result = put(state, args["path"], raw)
        if files[args["file_id"]].get("read_only"):
            state.setdefault("workspace_readonly", {})[result["path"]] = {
                "file_id": args["file_id"],
                "sha256": result["sha256"],
            }
            result["read_only"] = True
        return result
    if name == "workspace_write":
        if not isinstance(args["content"], str):
            raise caps.ToolError("파일 내용은 문자열이어야 합니다.")
        return put(state, args["path"], args["content"].encode())
    if name in {
        "workspace_read",
        "workspace_delete",
        "workspace_patch",
        "workspace_candidate",
    }:
        path = path_name(args["path"])
        if path not in workspace:
            raise caps.ToolError("작업 공간에 없는 파일입니다.")
        raw = base64.b64decode(workspace[path])
        if name != "workspace_read" and path in state.get("workspace_readonly", {}):
            raise caps.ToolError(
                "채점 기준 파일은 수정·삭제·솔루션 후보 등록을 할 수 없습니다."
            )
        if name == "workspace_delete":
            del workspace[path]
            return {"deleted": path}
        if name == "workspace_read":
            start, length = args["offset"], args["length"]
            if (
                type(start) is not int
                or start < 0
                or type(length) is not int
                or not 1 <= length <= 12000
            ):
                raise caps.ToolError("offset >= 0, length 1~12000을 지정하세요.")
            return {
                "path": path,
                "content": raw[start : start + length].decode(
                    "utf-8", errors="replace"
                ),
                "bytes": len(raw),
                "next_offset": start + length if start + length < len(raw) else None,
            }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise caps.ToolError("이 파일은 UTF-8 코드 파일이 아닙니다.") from None
        if name == "workspace_patch":
            old, new = args["old"], args["new"]
            if (
                not isinstance(old, str)
                or not old
                or not isinstance(new, str)
                or text.count(old) != 1
            ):
                raise caps.ToolError(
                    "교체할 문자열은 파일에 정확히 한 번 있어야 합니다."
                )
            return put(state, path, text.replace(old, new, 1).encode())
        if (
            args["language"] not in {"c99", "cpp17", "python311", "python313", "java8", "pypy3", "kotlin"}
            or len(raw) > caps.MAX_SOURCE
            or len(state["artifacts"]) >= 4
        ):
            raise caps.ToolError(
                "지원 언어 및 수정 후보 한도(4개, 128 KiB)를 확인하세요."
            )
        key = "candidate-" + str(len(state["artifacts"]) + 1)
        state["artifacts"][key] = {
            "source": text,
            "language": args["language"],
            "base": "workspace:" + path,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        return {
            "artifact_id": key,
            "next": "run_code로 최종 후보를 기존 채점기의 전체 등록 테스트에서 확인하세요.",
        }
    if name == "workspace_exec":
        if not configured():
            raise caps.ToolError(
                "별도 플레이그라운드가 아직 연결되지 않았습니다. 파일 편집과 기존 채점기 실행 도구는 사용할 수 있습니다."
            )
        executions = state.setdefault("playground_runs", [])
        command, seconds = args["command"], args["timeout_seconds"]
        if (
            not isinstance(command, str)
            or not 1 <= len(command.encode()) <= 8000
            or type(seconds) is not int
            or not 1 <= seconds <= 30
        ):
            raise caps.ToolError(
                "명령은 8000바이트 이하, 실행 제한은 1~30초로 지정하세요."
            )
        readonly = sorted(state.get("workspace_readonly", {}))
        request_id = ai.digest(
            [row.analysis_id, call_id, workspace, command, seconds, readonly]
        )
        existing = next((r for r in executions if r["request_id"] == request_id), None)
        if existing:
            return existing
        attempts = state.setdefault("playground_attempt_ids", [])
        maximum = max(
            1, min(48, state.get("limits", {}).get("max_playground_runs", 12))
        )
        if request_id not in attempts and len(attempts) >= maximum:
            raise caps.ToolError(
                f"분석당 플레이그라운드 실행 {maximum}회 한도에 도달했습니다."
            )
        if request_id not in attempts:
            attempts.append(request_id)
        payload = {
            "request_id": request_id,
            "files": workspace,
            "executables": [
                name
                for name in state.get("workspace_executables", [])
                if name in workspace
            ],
            "command": command,
            "readonly": readonly,
            "timeout_seconds": seconds,
        }
        try:
            response = httpx.post(
                settings.verification_playground_url.rstrip("/") + "/execute",
                headers={
                    "Authorization": "Bearer "
                    + settings.verification_playground_token.get_secret_value()
                },
                json=payload,
                timeout=httpx.Timeout(45, connect=3),
                follow_redirects=False,
            )
        except httpx.HTTPError:
            # The controller's idempotency key lets a worker retry the same call
            # without executing it twice. Do not automatically make a model call.
            tries = state.setdefault("playground_retries", {})
            tries[request_id] = tries.get(request_id, 0) + 1
            if tries[request_id] <= 2:
                return None
            raise caps.ToolError(
                "플레이그라운드 응답을 받지 못했습니다. 같은 실행 ID로 중복 실행을 방지했습니다."
            ) from None
        if response.status_code == 429:
            return None
        if response.status_code != 200:
            raise caps.ToolError(
                "격리 플레이그라운드 실행이 실패했습니다. 런타임·이미지·실행 제한을 확인하세요."
            )
        data = response.json()
        if data.get("runtime") != "gvisor" or data.get("network") != "disabled":
            raise caps.ToolError("요구한 격리 실행 환경이 아닙니다.")
        changed = data.get("files")
        if not isinstance(changed, dict):
            raise caps.ToolError("실행 파일 결과가 올바르지 않습니다.")
        new_state = {"workspace": {}}
        try:
            for path, encoded in changed.items():
                put(new_state, path, base64.b64decode(encoded, validate=True))
        except (TypeError, ValueError):
            raise caps.ToolError("작업 파일 결과 검증에 실패했습니다.") from None
        if readonly and (
            not data.get("readonly_enforced")
            or any(new_state["workspace"].get(p) != workspace.get(p) for p in readonly)
        ):
            raise caps.ToolError(
                "읽기 전용 채점 파일 보호를 확인하지 못해 실행 결과를 채택하지 않습니다."
            )
        state["workspace"] = new_state["workspace"]
        state["workspace_executables"] = [
            name for name in data.get("executables", []) if name in state["workspace"]
        ]
        result = {
            k: data.get(k)
            for k in (
                "exit_code",
                "timed_out",
                "stdout",
                "output_truncated",
                "wall_ms",
                "notes",
                "runtime",
                "network",
            )
        }
        result.update(request_id=request_id, command=command, files=manifest(state))
        result["stdout"] = str(result.get("stdout") or "")[:12000]
        executions.append(result)
        state["phase"] = "플레이그라운드 결과 검토"
        return result
    raise caps.ToolError("지원하지 않는 작업 공간 도구입니다.")
