"""Editor execution wire contract. Vendored identically in the three services."""
import base64
import hashlib
import io
import json
import math
import re
import stat
import zipfile

PROTOCOL = "editor-execute-v1"
FORMAT = "zoj-editor-execute-v1"
MAX_FILES_BYTES = 512 * 1024**2
MAX_WIRE_BYTES = 768 * 1024**2
KINDS = {"prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"}
STATUSES = {"ok", "time-limit", "memory-limit", "wall-limit", "output-limit"}


def path(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid execution path")
    parts = value.split("/")
    if value.startswith("/") or any(p in {"", ".", ".."} for p in parts) or any(p.startswith(".") for p in parts):
        if value != ".input":
            raise ValueError("Unsafe execution path")
    return value


def number(value, lower, upper, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper or (integer and not isinstance(value, int)):
        raise ValueError("Invalid execution limit or metric")
    return value


def command(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 128 or not value[0] or any(not isinstance(v, str) or len(v) > 4096 or "\x00" in v for v in value):
        raise ValueError("Invalid execution command")
    return value


def config(value):
    required = {"command", "cpu_ms", "memory_mb", "output_bytes", "wall_seconds", "output_file", "compile_language", "export_files"}
    optional = {"interactive", "interactor_command"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("Invalid execution configuration")
    command(value["command"])
    number(value["cpu_ms"], 1, 60000, True)
    number(value["memory_mb"], 4, 4096, True)
    number(value["output_bytes"], 1, MAX_FILES_BYTES, True)
    number(value["wall_seconds"], 0.01, 600)
    if value["compile_language"] not in {None, "cpp17", "cpp20", "c17", "c99", "java17", "java8", "python3", "pypy3", "kotlin"}:
        raise ValueError("Invalid compiler language")
    if value["output_file"] != "stdout":
        path(value["output_file"])
    if not isinstance(value["export_files"], list) or len(value["export_files"]) > 10:
        raise ValueError("Invalid export list")
    for name in value["export_files"]:
        path(name)
    if "interactive" in value and not isinstance(value["interactive"], bool):
        raise ValueError("Invalid interactive mode")
    if value.get("interactive"):
        command(value.get("interactor_command"))
    elif "interactor_command" in value:
        raise ValueError("Interactor requires interactive mode")
    return value


def encoded(value, limit):
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise ValueError("Encoded file exceeds limit")
    content = base64.b64decode(value, validate=True)
    if len(content) > limit:
        raise ValueError("File exceeds limit")
    return content


def validate_payload(value):
    if not isinstance(value, dict) or set(value) != {"format", "operation_id", "job_kind", "runtime", "config", "files"}:
        raise ValueError("Invalid execution payload")
    if value["format"] != FORMAT or not re.fullmatch(r"[a-f0-9]{32}", value["operation_id"]) or value["job_kind"] not in KINDS or value["runtime"] not in {"runtime", "renderer"}:
        raise ValueError("Unsupported execution request")
    config(value["config"])
    files = value["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= 2000:
        raise ValueError("Invalid execution files")
    total = 0
    for name, entry in files.items():
        path(name)
        if not isinstance(entry, dict) or set(entry) != {"base64", "executable"} or not isinstance(entry["executable"], bool):
            raise ValueError("Invalid file entry")
        total += len(encoded(entry["base64"], MAX_FILES_BYTES))
        if total > MAX_FILES_BYTES:
            raise ValueError("Execution files exceed 512MiB")
    if ".input" not in files:
        raise ValueError("Missing standard input")
    return value


def pack(value):
    validate_payload(value)
    blobs, files = {}, {}
    for name, entry in value["files"].items():
        content = encoded(entry["base64"], MAX_FILES_BYTES)
        sha = hashlib.sha256(content).hexdigest()
        blobs[sha] = content
        files[name] = {"sha256": sha, "executable": entry["executable"]}
    manifest = {**value, "files": files}
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        for sha, content in sorted(blobs.items()):
            archive.writestr("blobs/" + sha, content)
    return target.getvalue()


def unpack(content):
    if len(content) > MAX_WIRE_BYTES:
        raise ValueError("Execution archive exceeds limit")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2001 or len({e.filename for e in entries}) != len(entries) or sum(e.file_size for e in entries) > MAX_FILES_BYTES + 2 * 1024**2:
            raise ValueError("Invalid archive size or entries")
        for entry in entries:
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if entry.flag_bits & 1 or mode not in {0, stat.S_IFREG} or (entry.filename != "manifest.json" and not re.fullmatch(r"blobs/[a-f0-9]{64}", entry.filename)):
                raise ValueError("Unsafe archive entry")
        if archive.getinfo("manifest.json").file_size > 2 * 1024**2:
            raise ValueError("Manifest exceeds limit")
        value = json.loads(archive.read("manifest.json"))
        hashes = set()
        files = {}
        for name, entry in value["files"].items():
            if set(entry) != {"sha256", "executable"} or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
                raise ValueError("Invalid file reference")
            sha = entry["sha256"]
            data = archive.read("blobs/" + sha)
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("File integrity mismatch")
            hashes.add("blobs/" + sha)
            files[name] = {"base64": base64.b64encode(data).decode(), "executable": entry["executable"]}
        if {e.filename for e in entries} != {"manifest.json", *hashes}:
            raise ValueError("Unexpected archive contents")
        return validate_payload({**value, "files": files})


def validate_result(value, limits):
    allowed = {"status", "exit_code", "cpu_ms", "wall_ms", "memory_kb", "stopped_by_interactor", "interactor_exit_code", "interactor_log", "stderr", "output_base64", "artifacts", "metrics_available"}
    if not isinstance(value, dict) or set(value) - allowed or value.get("status") not in STATUSES:
        raise ValueError("Invalid sandbox result")
    for metric in ("cpu_ms", "wall_ms", "memory_kb"):
        if value.get(metric) is not None:
            number(value[metric], 0, 10**12)
    number(value["exit_code"], -65536, 65536, True)
    for key in ["stderr", "interactor_log"]:
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 32768):
            raise ValueError("Execution log exceeds limit")
    encoded(value.get("output_base64", ""), limits["output_bytes"])
    artifacts = value.get("artifacts", {})
    if not isinstance(artifacts, dict) or len(artifacts) > 2000:
        raise ValueError("Invalid artifacts")
    total = 0
    language = limits["compile_language"]
    for name, content in artifacts.items():
        path(name)
        compiler_artifact = language in {"cpp17", "cpp20", "c17", "c99"} and name == "program" or language in {"java17", "java8"} and name.endswith(".class") or language == "kotlin" and name == "program.jar"
        if not compiler_artifact and name not in limits["export_files"]:
            raise ValueError("Unexpected execution artifact")
        total += len(encoded(content, 32 * 1024**2))
        if total > 32 * 1024**2:
            raise ValueError("Artifacts exceed limit")
    return value
