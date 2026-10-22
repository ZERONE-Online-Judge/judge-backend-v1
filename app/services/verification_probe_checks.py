"""Cross-check proposed inputs with immutable registered validator/reference code."""

import copy
import json
from pathlib import PurePosixPath

from app.services import verification_ai as ai, verification_agent_tools as caps
from app.services import verification_workspace as workspace

# Executed only by the existing gVisor service. No source runs in the API worker.
DRIVER = r"""
import json, subprocess
from pathlib import Path
cfg=json.loads(Path("config.json").read_text())
def execute(command, seconds, input_path=None):
    try:
        with open("stdout.tmp", "wb") as out, open("stderr.tmp", "wb") as err:
            with open(input_path or "/dev/null", "rb") as inp:
                proc=subprocess.run(command, stdin=inp, stdout=out, stderr=err, timeout=seconds)
        with open("stdout.tmp", "rb") as f: output=f.read(8193)
        with open("stderr.tmp", "rb") as f: error=f.read(1000)
        return {"exit_code":proc.returncode, "output":output[:8192].decode(errors="replace"), "output_truncated":len(output)>8192, "error":error.decode(errors="replace")}
    except subprocess.TimeoutExpired:
        return {"exit_code":None, "error":"timeout", "output":"", "output_truncated":False}
def run(role):
    item=cfg[role]
    if not item: return {"status":"unavailable"}
    if item["language"] in ("cpp17","c99"):
        compiler="g++" if item["language"]=="cpp17" else "gcc"
        built=execute([compiler,"-std="+item["language"].replace("cpp","c++"),"-O2","-I.",item["path"],"-o",role+".bin"], 15)
        if built["exit_code"]!=0: return {"status":"compile_error", "details":built}
        command=["./"+role+".bin"]
    else: command=["python3","-I",item["path"]]
    result=execute(command, 5, "input.txt")
    return {"status":"executed", **result}
validator=run("validator")
reference=run("reference")
expected=Path("expected.txt").read_text()
matched=reference.get("exit_code")==0 and not reference.get("output_truncated") and reference["output"].split()==expected.split()
print("ZOJ_PROBE_CHECK:"+json.dumps({"validator":validator,"reference":reference,"expected_matches_reference":matched},ensure_ascii=False))
"""


def check(row, context, state, files, args, call):
    for key in ("input", "expected_output"):
        if not isinstance(args[key], str) or len(args[key].encode()) > 8192:
            raise caps.ToolError(
                "입력·기대 출력은 각각 8 KiB 이하 문자열이어야 합니다."
            )
    if not workspace.configured():
        raise caps.ToolError("반례 교차 검증에는 격리 플레이그라운드가 필요합니다.")
    check_id = ai.digest([row.context_hash, args])
    existing = next(
        (r for r in state.get("probe_checks", []) if r["check_id"] == check_id), None
    )
    if existing:
        return existing
    folder = "checks/" + check_id[:12]
    if not call.get("probe_prepared"):
        prepared = copy.deepcopy(state)
        config = {}
        for role in ("validator", "reference"):
            fid = args[role + "_id"]
            if not fid:
                config[role] = None
                continue
            item = files.get(fid, {})
            required = (
                "/validator/"
                if role == "validator"
                else "/verification-solutions/accepted/"
            )
            if required not in item.get("storage_key", ""):
                raise caps.ToolError(
                    "validator는 등록 validator, reference는 등록된 정답 의도 검증 풀이의 파일 ID를 지정하세요. 없으면 빈 문자열로 남기고 한계를 보고하세요."
                )
            ext = PurePosixPath(item["name"]).suffix.lower()
            language = {
                ".cpp": "cpp17",
                ".cc": "cpp17",
                ".c": "c99",
                ".py": "python311",
            }.get(ext)
            if not language:
                raise caps.ToolError(
                    "자동 반례 교차 검증은 C/C++·Python 등록 코드를 지원합니다. 다른 환경은 workspace_exec로 검토하세요."
                )
            path = role + ext
            protect(prepared, folder + "/" + path, caps.file_bytes(files, fid), fid)
            config[role] = {"path": path, "language": language}
        # Include only registered headers, never fetch arbitrary external libraries.
        headers = {}
        for fid, item in files.items():
            name = PurePosixPath(item.get("name", "")).name
            if name.endswith((".h", ".hpp")) and item.get("read_only"):
                if name in headers and headers[name] != item.get("sha256"):
                    raise caps.ToolError(
                        "같은 이름의 서로 다른 헤더가 있습니다. 정확한 빌드 구성을 먼저 확인하세요."
                    )
                if name not in headers:
                    protect(
                        prepared, folder + "/" + name, caps.file_bytes(files, fid), fid
                    )
                headers[name] = item.get("sha256")
        for name, text in {
            "input.txt": args["input"],
            "expected.txt": args["expected_output"],
            "config.json": json.dumps(config),
            "check.py": DRIVER,
        }.items():
            protect(
                prepared, folder + "/" + name, text.encode(), "probe-check:" + check_id
            )
        state["workspace"] = prepared["workspace"]
        state["workspace_readonly"] = prepared.get("workspace_readonly", {})
        call["probe_prepared"] = True
    result = workspace.handle(
        row,
        context,
        state,
        files,
        "workspace_exec",
        {"command": "cd " + folder + " && python3 -I check.py", "timeout_seconds": 30},
        call["call_id"],
    )
    if result is None:
        return None
    details = None
    if result.get("exit_code") == 0 and not result.get("output_truncated"):
        lines = (result.get("stdout") or "").splitlines()
        if lines and lines[-1].startswith("ZOJ_PROBE_CHECK:"):
            try:
                details = json.loads(lines[-1].split(":", 1)[1])
            except ValueError:
                pass
    status = "incomplete"
    if (
        isinstance(details, dict)
        and isinstance(details.get("validator"), dict)
        and isinstance(details.get("reference"), dict)
    ):
        if (
            details.get("validator", {}).get("exit_code") == 0
            and details.get("expected_matches_reference") is True
        ):
            status = "cross_checked"
        elif details.get("validator", {}).get("exit_code") not in (None, 0) or (
            details.get("reference", {}).get("exit_code") == 0
            and not details.get("expected_matches_reference")
        ):
            status = "conflict"
    saved = {
        "check_id": check_id,
        "probe_hash": ai.digest({k: args[k] for k in ("input", "expected_output")}),
        "status": status,
        "input": args["input"],
        "expected_output": args["expected_output"],
        "validator_id": args["validator_id"],
        "reference_id": args["reference_id"],
        "details": details,
        "experiment_id": result["request_id"],
        "note": "등록 validator와 정답 의도 참조 풀이를 원본 그대로 실행한 교차 확인입니다. 참조 풀이의 수학적 정확성을 증명하지 않으며, 출력 비교는 공백 토큰 기준입니다. 기존 checker를 통한 원본 반례 실행은 run_probe로 별도 확인하세요.",
    }
    state.setdefault("probe_checks", []).append(saved)
    # These private tool-owned build files are disposable; evidence and immutable
    # registered originals remain stored. Avoid filling the shared workspace.
    for path in list(state.get("workspace", {})):
        if path.startswith(folder + "/"):
            state["workspace"].pop(path, None)
            state.get("workspace_readonly", {}).pop(path, None)
    state["workspace_executables"] = [
        p
        for p in state.get("workspace_executables", [])
        if not p.startswith(folder + "/")
    ]
    return saved


def protect(state, path, data, fid):
    # Retry-safe preparation without replacing any existing protected content.
    import base64

    existing = state.get("workspace", {}).get(path)
    if existing is not None:
        if base64.b64decode(existing) != data:
            raise caps.ToolError("교차 검증 파일 내용이 달라 준비를 중단했습니다.")
    else:
        workspace.put(state, path, data)
    import hashlib

    state.setdefault("workspace_readonly", {})[path] = {
        "file_id": fid,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
