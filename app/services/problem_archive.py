"""Portable problem archives. Only data is imported; no archived code is executed."""
from __future__ import annotations
from pydantic import model_serializer

import hashlib
import json
import logging
import re
import stat
import struct
import zlib
import tempfile
import zipfile
from contextlib import contextmanager, nullcontext
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from app.problem_languages import ProblemStatements, validate_statement_fields
from app.judge_languages import LANGUAGES, ProgrammingLanguages
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import Problem, now_utc
from app.orm_models import BundleWarmQueueItemRow, ContestDivisionRow, ContestRow, ProblemAssetRow, ProblemRow, TestcaseRow, TestcaseSetRow
from app.services.errors import AppError, not_found
from app.services.storage import object_storage

MAX_UPLOAD = 512 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
MAX_ENTRIES = 20_000
CHUNK = 1024 * 1024
PACKAGE_ROLES = {'main-solution', 'brute-solution', 'wrong-solution', 'checker', 'validator',
                 'generator', 'manual-input', 'test-script', 'package-resource', 'interactor'}
VERIFICATION_KINDS = {'accepted', 'wrong_answer', 'time_limit_exceeded', 'memory_limit_exceeded'}
log = logging.getLogger(__name__)


def invalid(message: str):
    return AppError(422, 'problem_archive_invalid', message)


class ArchiveModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ResourceLimit(ArchiveModel):
    disable_auto_adjustment: bool = False

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value=handler(self)
        if not self.disable_auto_adjustment: value.pop("disable_auto_adjustment", None)
        return value

    time_limit_ms: StrictInt | None = Field(default=None, ge=100, le=10000)
    memory_limit_mb: StrictInt | None = Field(default=None, ge=16, le=1024)


class ArchiveProblem(ProblemStatements, ProgrammingLanguages, ArchiveModel):
    problem_code: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=255)
    statement: str = Field(max_length=4 * 1024 * 1024)
    editorial: str = Field(default='', max_length=4 * 1024 * 1024)
    time_limit_ms: StrictInt = Field(ge=100, le=10000)
    memory_limit_mb: StrictInt = Field(ge=16, le=1024)
    language_resource_limits: dict[str, ResourceLimit] = Field(default_factory=dict, max_length=6)
    display_order: StrictInt = Field(ge=1, le=1_000_000)

    @field_validator('language_resource_limits')
    @classmethod
    def known_languages(cls, value):
        if set(value) - set(LANGUAGES):
            raise ValueError('Unknown language')
        return value


class ArchiveFile(ArchiveModel):
    path: str = Field(min_length=1, max_length=240)
    size: StrictInt = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ArchiveAsset(ArchiveModel):
    id: str = Field(pattern=r'^asset-[0-9]+$', max_length=32)
    file: str = Field(max_length=240)
    original_filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=120)
    category: str = Field(max_length=80)
    source_language: Literal['c99', 'cpp17', 'python313', 'java8', 'pypy3', 'kotlin'] | None = None
    asset_status: str = Field(default='active', min_length=1, max_length=32)
    created_at: str = Field(max_length=40)

    @field_validator('original_filename')
    @classmethod
    def filename(cls, value):
        if value in {'.', '..'} or any(ord(c) < 32 or c in '/\\' for c in value):
            raise ValueError('Invalid filename')
        return value

    @field_validator('created_at')
    @classmethod
    def valid_date(cls, value):
        from datetime import datetime
        datetime.fromisoformat(value)
        return value

    @field_validator('category')
    @classmethod
    def category_allowed(cls, value):
        if value != 'assets' and value not in {f'package-files/{r}' for r in PACKAGE_ROLES} and value not in {f'verification-solutions/{k}' for k in VERIFICATION_KINDS}:
            raise ValueError('Unknown asset category')
        return value


class ArchiveCase(ArchiveModel):
    display_order: StrictInt = Field(ge=1, le=1_000_000)
    input: str = Field(max_length=240)
    output: str = Field(max_length=240)
    time_limit_ms_override: StrictInt | None = Field(default=None, ge=100, le=10000)
    memory_limit_mb_override: StrictInt | None = Field(default=None, ge=16, le=1024)


class ArchiveSet(ArchiveModel):
    version: StrictInt = Field(ge=1, le=1_000_000)
    is_active: bool
    testcases: list[ArchiveCase] = Field(max_length=10000)


class Manifest(ArchiveModel):
    format: Literal['zoj-problem']
    version: Literal[1]
    problem: ArchiveProblem
    assets: list[ArchiveAsset] = Field(max_length=10000)
    testcase_sets: list[ArchiveSet] = Field(max_length=1000)
    files: list[ArchiveFile] = Field(max_length=MAX_ENTRIES - 1)


def _category(key: str) -> str:
    for category in [*(f'package-files/{r}' for r in PACKAGE_ROLES), *(f'verification-solutions/{k}' for k in VERIFICATION_KINDS)]:
        if f'/{category}/' in key:
            return category
    for role in PACKAGE_ROLES:
        if f'/support/{role}/' in key:
            return f'package-files/{role}'
    return 'assets'


def _rewrite_document(text: str, ids: dict[str, str], storage: dict[str, str] | None = None) -> str:
    text = re.sub(r'asset://([A-Za-z0-9-]+)', lambda m: 'asset://' + ids.get(m[1], m[1]), text)
    if storage:
        # Convert expired signed storage URLs as well as unsigned relative URLs.
        pattern = r'(?:https?://[^\s/"<>]+)?/(?:api/)?storage/objects/[^\s)"<>\\]+'
        def replace(match):
            url = match[0]
            key = unquote(re.split(r'/storage/objects/', url, maxsplit=1)[-1].split('?')[0].split('#')[0])
            return 'asset://' + storage[key] if key in storage else url
        text = re.sub(pattern, replace, text)
    return text


def rewrite_problem_documents(values, ids, storage=None):
    for field in ("statement", "editorial"):
        values[field] = _rewrite_document(values.get(field) or "", ids, storage)
    values["statement_translations"] = {language: {**item,
        "statement": _rewrite_document(item["statement"], ids, storage)}
        for language, item in (values.get("statement_translations") or {}).items()}
    values["editorial_translations"] = {language: _rewrite_document(text, ids, storage)
        for language, text in (values.get("editorial_translations") or {}).items()}
    return values


def problem_document_texts(problem):
    return [problem.statement, problem.editorial,
            *(item.statement for item in problem.statement_translations.values()),
            *problem.editorial_translations.values()]


def _safe_zip_path(name: str):
    if not name or len(name) > 240 or name.startswith('/') or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
        raise invalid('압축 파일에 허용되지 않는 경로가 있습니다.')
    if any(part in {'', '.', '..'} for part in name.split('/')):
        raise invalid('압축 파일 경로는 상위 폴더를 참조할 수 없습니다.')


def _unique_json(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('Duplicate manifest field')
        data[key] = value
    return data


def _validate_references(manifest: Manifest):
    validate_statement_fields(manifest.problem.default_statement_language,
        manifest.problem.statement_translations, manifest.problem.editorial_translations)
    files = {file.path: file for file in manifest.files}
    asset_ids = [asset.id for asset in manifest.assets]
    versions = [item.version for item in manifest.testcase_sets]
    if sum(len(item.testcases) for item in manifest.testcase_sets) > 10000:
        raise invalid('테스트케이스는 전체 10,000개 이하만 지원합니다.')
    if len(set(asset_ids)) != len(asset_ids) or len(set(versions)) != len(versions) or sum(item.is_active for item in manifest.testcase_sets) > 1:
        raise invalid('첨부 파일 ID, 테스트케이스 버전 또는 활성 상태가 중복되었습니다.')
    referenced = {asset.file for asset in manifest.assets}
    for item in manifest.testcase_sets:
        orders = [case.display_order for case in item.testcases]
        if len(set(orders)) != len(orders):
            raise invalid('한 테스트케이스 버전에 중복된 순서가 있습니다.')
        for case in item.testcases:
            referenced.update([case.input, case.output])
    if referenced != set(files):
        raise invalid('문제에 연결되지 않았거나 누락된 파일이 있습니다.')
    for text in problem_document_texts(manifest.problem):
        if set(re.findall(r'asset://([A-Za-z0-9-]+)', text)) - set(asset_ids):
            raise invalid('지문이나 해설에 누락된 첨부 파일 참조가 있습니다.')


@contextmanager
def read_archive(source):
    """Validate the complete archive before creating any rows or objects."""
    archive = None
    try:
        source.seek(0, 2)
        if source.tell() > MAX_UPLOAD:
            raise AppError(413, 'problem_archive_too_large', '문제 ZIP은 512 MiB 이하만 지원합니다.')
        # Reject oversized directories before zipfile allocates per-entry objects.
        source.seek(max(0, source.tell() - 65557))
        trailer = source.read()
        end = trailer.rfind(b'PK\x05\x06')
        if end < 0 or len(trailer) - end < 22:
            raise invalid('ZIP 종료 정보가 없습니다.')
        _, disk, directory_disk, disk_entries, entries_count, directory_size, _, comment_size = struct.unpack('<4s4H2LH', trailer[end:end + 22])
        if disk or directory_disk or disk_entries != entries_count or entries_count > MAX_ENTRIES or directory_size > MAX_ENTRIES * 1024 or len(trailer) - end != 22 + comment_size:
            raise invalid('ZIP 파일 목록이 한도를 초과하거나 형식이 올바르지 않습니다.')
        source.seek(0)
        archive = zipfile.ZipFile(source)
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise invalid('ZIP의 파일 수가 허용 한도를 초과했습니다.')
        names = set()
        total = 0
        for info in entries:
            _safe_zip_path(info.filename)
            mode = info.external_attr >> 16
            if info.filename in names or info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise invalid('중복 파일·디렉터리·특수 파일은 허용하지 않습니다.')
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise invalid('암호화 또는 지원하지 않는 방식의 ZIP입니다.')
            if info.file_size > (MAX_MANIFEST if info.filename == 'manifest.json' else MAX_FILE):
                raise invalid('개별 파일 크기 한도를 초과했습니다.')
            total += info.file_size
            if total > MAX_UNPACKED:
                raise invalid('압축 해제 후 전체 크기는 2 GiB 이하만 지원합니다.')
            names.add(info.filename)
        if 'manifest.json' not in names:
            raise invalid('ZOJ 문제 ZIP이 아닙니다. manifest.json이 필요합니다.')
        raw = json.loads(archive.read('manifest.json'), object_pairs_hook=_unique_json)
        if isinstance(raw, dict) and (raw.get('format') != 'zoj-problem' or type(raw.get('version')) is not int or raw.get('version') != 1):
            raise invalid('지원하지 않는 문제 ZIP 형식 또는 버전입니다.')
        manifest = Manifest.model_validate(raw)
        files = {file.path: file for file in manifest.files}
        if len(files) != len(manifest.files) or names != {'manifest.json', *files}:
            raise invalid('파일 목록과 ZIP 내용이 일치하지 않습니다.')
        _validate_references(manifest)
        for name, file in files.items():
            if archive.getinfo(name).file_size != file.size:
                raise invalid('파일 크기가 파일 목록과 일치하지 않습니다.')
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as content:
                while chunk := content.read(CHUNK):
                    size += len(chunk)
                    if size > file.size:
                        raise invalid('파일이 선언된 크기를 초과했습니다.')
                    digest.update(chunk)
            if size != file.size or digest.hexdigest() != file.sha256:
                raise invalid('파일이 손상되었거나 무결성 검사에 실패했습니다.')
        yield archive, manifest
    except (zipfile.BadZipFile, UnicodeError, ValueError, ValidationError, KeyError, NotImplementedError, RuntimeError, OverflowError, zlib.error, EOFError, RecursionError) as error:
        raise invalid('문제 ZIP이 손상되었거나 형식이 올바르지 않습니다.') from error
    finally:
        if archive:
            archive.close()


def summary(manifest: Manifest):
    document = re.match(r'^<!--ZOJ_META:(.+?)-->\n?', manifest.problem.statement, re.S)
    examples = 0
    if document:
        try:
            meta = json.loads(document[1])
            examples = len(meta.get('examples', [])) if isinstance(meta, dict) and isinstance(meta.get('examples', []), list) else 0
        except (ValueError, TypeError):
            pass
    return {'problem': manifest.problem.model_dump(), 'asset_count': len(manifest.assets),
            'file_count': len(manifest.files), 'total_bytes': sum(file.size for file in manifest.files),
            'example_count': examples, 'testcase_count': sum(len(item.testcases) for item in manifest.testcase_sets),
            'testcase_sets': [{'version': item.version, 'is_active': item.is_active, 'count': len(item.testcases)} for item in manifest.testcase_sets],
            'assets': [{'filename': item.original_filename, 'category': item.category} for item in manifest.assets],
            'statement_languages': [manifest.problem.default_statement_language, *manifest.problem.statement_translations],
            'has_external_links': any(re.search(r'https?://', text) for text in problem_document_texts(manifest.problem))}


def inspect_archive(source):
    with read_archive(source) as (_, manifest):
        return summary(manifest)


def export_archive(contest_id: str, problem_id: str, *, session=None):
    target = tempfile.TemporaryFile('w+b')
    try:
        with (SessionLocal() if session is None else nullcontext(session)) as db:
            problem = db.scalar(select(ProblemRow).where(ProblemRow.contest_id == contest_id, ProblemRow.problem_id == problem_id))
            if not problem:
                raise not_found()
            assets = db.scalars(select(ProblemAssetRow).where(ProblemAssetRow.contest_id == contest_id, ProblemAssetRow.problem_id == problem_id).order_by(ProblemAssetRow.created_at, ProblemAssetRow.asset_id)).all()
            sets = db.scalars(select(TestcaseSetRow).where(TestcaseSetRow.problem_id == problem_id).order_by(TestcaseSetRow.version)).all()
            ids = {asset.asset_id: f'asset-{index}' for index, asset in enumerate(assets, 1)}
            storage_ids = {asset.storage_key: ids[asset.asset_id] for asset in assets}
            metadata = {field: getattr(problem, field) for field in ArchiveProblem.model_fields}
            rewrite_problem_documents(metadata, ids, storage_ids)
            metadata['language_resource_limits'] = metadata['language_resource_limits'] or {}
            data = {'format': 'zoj-problem', 'version': 1, 'problem': metadata, 'assets': [], 'testcase_sets': [], 'files': []}
            total_size = 0
            copied = {}
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                def add_file(key, name, expected_hash):
                    nonlocal total_size
                    if key in copied:
                        if expected_hash and copied[key][1] != expected_hash.lower():
                            raise invalid('같은 저장 파일의 체크섬 정보가 서로 다릅니다.')
                        return copied[key][0]
                    object_storage.validate_key(key)
                    if not key.startswith(f'contests/{contest_id}/'):
                        raise invalid('문제 파일이 다른 대회의 저장 공간을 참조합니다.')
                    if len(data['files']) + 1 >= MAX_ENTRIES:
                        raise invalid('문제 파일 수가 내보내기 한도를 초과했습니다.')
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        with object_storage.open_reader(key) as source, archive.open(name, 'w', force_zip64=True) as output:
                            while chunk := source.read(CHUNK):
                                size += len(chunk)
                                total_size += len(chunk)
                                if size > MAX_FILE or total_size > MAX_UNPACKED:
                                    raise invalid('파일 크기가 문제 ZIP 한도를 초과했습니다.')
                                digest.update(chunk)
                                output.write(chunk)
                    except AppError:
                        raise
                    except Exception as error:
                        raise AppError(409, 'problem_archive_missing_file', '문제에 연결된 파일을 읽을 수 없습니다. 파일을 복구한 뒤 다시 내보내 주세요.') from error
                    checksum = digest.hexdigest()
                    if expected_hash and checksum != expected_hash.lower():
                        raise AppError(409, 'problem_archive_checksum_mismatch', '저장된 파일의 체크섬이 다릅니다. 파일을 확인한 뒤 다시 내보내 주세요.')
                    data['files'].append({'path': name, 'size': size, 'sha256': checksum})
                    copied[key] = (name, checksum)
                    return name
                for index, asset in enumerate(assets, 1):
                    name = f'assets/{index:05d}/{re.sub(r"[^a-zA-Z0-9._-]", "_", asset.original_filename)[:120] or "file"}'
                    data['assets'].append({'id': ids[asset.asset_id], 'file': add_file(asset.storage_key, name, asset.sha256),
                        'original_filename': asset.original_filename, 'mime_type': asset.mime_type, 'category': _category(asset.storage_key),
                        'source_language': asset.source_language, 'asset_status': asset.asset_status, 'created_at': asset.created_at.isoformat()})
                for item in sets:
                    cases = db.scalars(select(TestcaseRow).where(TestcaseRow.testcase_set_id == item.testcase_set_id).order_by(TestcaseRow.display_order)).all()
                    exported = {'version': item.version, 'is_active': item.is_active, 'testcases': []}
                    for index, case in enumerate(cases, 1):
                        exported['testcases'].append({'display_order': case.display_order,
                            'input': add_file(case.input_storage_key, f'testcases/v{item.version}/{index:05d}.in', case.input_sha256),
                            'output': add_file(case.output_storage_key, f'testcases/v{item.version}/{index:05d}.out', case.output_sha256),
                            'time_limit_ms_override': case.time_limit_ms_override, 'memory_limit_mb_override': case.memory_limit_mb_override})
                    data['testcase_sets'].append(exported)
                # Refuse to emit an archive that this version cannot import.
                manifest = Manifest.model_validate(data)
                _validate_references(manifest)
                encoded = json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2).encode()
                if len(encoded) > MAX_MANIFEST or total_size + len(encoded) > MAX_UNPACKED:
                    raise invalid('문제 메타데이터가 내보내기 한도를 초과했습니다.')
                archive.writestr('manifest.json', encoded)
            if target.tell() > MAX_UPLOAD:
                raise AppError(413, 'problem_archive_too_large', '압축된 문제 ZIP이 512 MiB를 초과했습니다.')
            target.seek(0)
            return target, f'{problem.problem_code}-{problem.title}.zoj.zip'
    except ValidationError as error:
        target.close()
        raise AppError(409, 'problem_archive_source_invalid', '기존 문제의 제한 값 또는 파일 정보가 ZIP 형식에 맞지 않습니다. 문제 설정을 확인해 주세요.') from error
    except Exception:
        target.close()
        raise


def import_archive(source, contest_id: str, division_id: str, problem_code: str, display_order: int | None = None, *, editor_context: dict | None = None, update_context: dict | None = None):
    code = problem_code.strip()
    if not code or len(code) > 16 or any(ord(c) < 32 for c in code):
        raise invalid('문제 번호는 1~16자로 입력해 주세요.')
    written = []
    committed = False
    try:
        with read_archive(source) as (archive, manifest), SessionLocal() as db:
            if editor_context or any(a.original_filename == 'editor-project.json' and a.category == 'package-files/package-resource' for a in manifest.assets):
                from app.services.package_updates import require_verified
                require_verified(manifest, archive)
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            division = db.scalar(select(ContestDivisionRow).where(ContestDivisionRow.division_id == division_id, ContestDivisionRow.contest_id == contest_id).with_for_update())
            if not contest or contest.deleted_at is not None or not division:
                raise not_found('참가 유형을 찾을 수 없습니다.')
            from app.services.store import _aware
            def ensure_unlocked():
                now = now_utc()
                if contest.status == 'running' or (contest.status not in {'draft', 'schedule_tbd', 'ended', 'finalized', 'archived'} and _aware(contest.start_at) <= now < _aware(contest.end_at)):
                    raise AppError(409, 'contest_locked', '대회 진행 중에는 문제를 가져올 수 없습니다.')
            if not update_context:
                ensure_unlocked()
            if editor_context:
                from app.services.editor_bridge import staff
                from app.services.contest_roles import title_for_roles
                from app.orm_models import EditorImportRow, OperationalAuditLogRow
                account = staff(db, editor_context["email"], contest_id, lock=True)
                receipt = db.get(EditorImportRow, editor_context["operation_key"])
                if receipt:
                    if receipt.request_digest != editor_context["request_digest"]:
                        raise AppError(409, "editor_operation_conflict", "같은 전송 요청의 내용이 변경되었습니다.")
                    existing = db.get(ProblemRow, receipt.problem_id)
                    if not existing or existing.contest_id != contest_id:
                        raise not_found()
                    return Problem(**{field: getattr(existing, field) for field in Problem.model_fields}).model_dump(mode='json')
            if update_context and not editor_context:
                raise AppError(403, 'package_update_untrusted', '인증된 Editor 패키지로 업데이트하세요.')
            if update_context:
                from app.services import package_updates
                problem = package_updates.target(db, contest_id, update_context['problem_id'], lock=True)
                package_updates.check_update(db, contest, problem, update_context)
                update_notice = package_updates.update_notice(contest, problem, manifest, update_context)
                if not any(s.is_active and s.testcases for s in manifest.testcase_sets):
                    raise AppError(409, 'package_update_no_tests', '활성 테스트가 있는 패키지를 선택하세요.')
                pid = problem.problem_id
                backup, _ = export_archive(contest_id, pid, session=db)
                backup_key = f'contests/{contest_id}/problems/{pid}/package-backups/{editor_context["operation_key"]}.zoj.zip'
                written.append(backup_key)
                try:
                    backup.seek(0, 2)
                    size = backup.tell()
                    backup.seek(0)
                    object_storage.write_stream(backup_key, backup, size, 'application/zip')
                except Exception as error:
                    raise AppError(503, 'package_backup_failed', '이전 패키지 백업에 실패해 업데이트를 취소했습니다.') from error
                finally:
                    backup.close()
                previous_sets = list(db.scalars(select(TestcaseSetRow).where(TestcaseSetRow.problem_id == pid).with_for_update()))
                version_base = max((s.version for s in previous_sets), default=0)
                for item in previous_sets:
                    item.is_active = False
                for asset in db.scalars(select(ProblemAssetRow).where(ProblemAssetRow.problem_id == pid).with_for_update()):
                    db.delete(asset)
            elif db.scalar(select(ProblemRow.problem_id).where(ProblemRow.contest_id == contest_id, ProblemRow.division_id == division_id, ProblemRow.problem_code == code)):
                raise AppError(409, 'problem_archive_code_conflict', '이 참가 유형에 같은 문제 번호가 있습니다. 다른 번호를 입력해 주세요.')
            else:
                pid = str(uuid4())
            asset_ids = {item.id: str(uuid4()) for item in manifest.assets}
            payload = manifest.problem.model_dump()
            payload.update(problem_id=pid, contest_id=contest_id, division_id=division_id, problem_code=code,
                           display_order=display_order or ((db.scalar(select(func.max(ProblemRow.display_order)).where(ProblemRow.contest_id == contest_id, ProblemRow.division_id == division_id)) or 0) + 1))
            rewrite_problem_documents(payload, asset_ids)
            if update_context:
                # ZOJ owns routing and language-specific contest policy.
                for field in package_updates.FIELDS:
                    setattr(problem, field, payload[field])
            else:
                problem = ProblemRow(**payload)
                db.add(problem)
            db.flush()
            file_by_path = {file.path: file for file in manifest.files}
            object_keys = {}
            def upload(path, category, filename, mime, *, asset_id=None):
                # Each asset row owns a unique storage key, even when several
                # manifest entries refer to the same ZIP member and role.
                pair = (path, category, asset_id)
                if pair in object_keys:
                    return object_keys[pair]
                key = f'contests/{contest_id}/problems/{pid}/{category}/{uuid4().hex}-{filename}'
                written.append(key)
                with archive.open(path) as content:
                    try:
                        object_storage.write_stream(key, content, file_by_path[path].size, mime)
                    except Exception as error:
                        raise AppError(503, 'problem_archive_storage_failed', '파일 저장에 실패해 등록을 취소했습니다. 잠시 후 다시 시도해 주세요.') from error
                object_keys[pair] = key
                return key
            # Preserve relative age/order for selecting the latest validator or solution.
            from datetime import datetime
            for item in manifest.assets:
                try:
                    created = datetime.fromisoformat(item.created_at)
                except ValueError:
                    raise invalid('첨부 파일의 생성 시각이 올바르지 않습니다.')
                file = file_by_path[item.file]
                db.add(ProblemAssetRow(asset_id=asset_ids[item.id], contest_id=contest_id, problem_id=pid,
                    original_filename=item.original_filename, storage_key=upload(item.file, item.category, item.original_filename, item.mime_type, asset_id=asset_ids[item.id]),
                    mime_type=item.mime_type, file_size=file.size, sha256=file.sha256, source_language=item.source_language, asset_status=item.asset_status, created_at=created))
            for position, item in enumerate(sorted(manifest.testcase_sets, key=lambda s: s.version), 1):
                set_id = str(uuid4())
                db.add(TestcaseSetRow(testcase_set_id=set_id, problem_id=pid, version=version_base + position if update_context else item.version, is_active=item.is_active))
                db.flush()
                for case in item.testcases:
                    db.add(TestcaseRow(testcase_set_id=set_id, display_order=case.display_order,
                        input_storage_key=upload(case.input, f'testcases/{set_id}', f'{case.display_order}.in', 'application/octet-stream'),
                        output_storage_key=upload(case.output, f'testcases/{set_id}', f'{case.display_order}.out', 'application/octet-stream'),
                        input_sha256=file_by_path[case.input].sha256, output_sha256=file_by_path[case.output].sha256,
                        time_limit_ms_override=case.time_limit_ms_override, memory_limit_mb_override=case.memory_limit_mb_override))
            # Persist the warm-up request with the problem so workers cannot see
            # incomplete files, and a failed import cannot leave a queued job.
            if any(item.is_active for item in manifest.testcase_sets):
                db.add(BundleWarmQueueItemRow(contest_id=contest_id, problem_id=pid))
            if not update_context:
                ensure_unlocked()
            if editor_context:
                db.add(EditorImportRow(operation_key=editor_context["operation_key"], request_digest=editor_context["request_digest"], email=account.email, problem_id=pid))
                if not editor_context.get("skip_audit"):
                    db.add(OperationalAuditLogRow(scope="operator", action="Editor 패키지 업데이트" if update_context else "Editor 문제 가져오기", method="POST",
                        path=editor_context.get('path', f"/api/editor/contests/{contest_id}/problems/{pid}/package-updates" if update_context else f"/api/editor/contests/{contest_id}/imports"), status_code=200, actor_email=account.email,
                        actor_name=account.display_name, actor_role="마스터" if account.is_service_master else title_for_roles(account.contest_roles.get(contest_id, [])), contest_id=contest_id, request_id=editor_context["request_id"],
                        details=json.dumps({"problem_id": pid, "title": problem.title, "problem_code": code,
                            "target": {"problem_title": problem.title, "problem_code": code},
                            "division_id": division_id, "testcase_count": sum(len(s.testcases) for s in manifest.testcase_sets),
                            **({"reason": update_context['reason'], "backup_key": backup_key, "previous_target_hash": update_context['target_hash'], "editor": editor_context.get('source', {})} if update_context else {})}, ensure_ascii=False)))
            if update_context and update_notice:
                from app.services.store import store
                store.create_contest_notice(contest_id, *update_notice, pinned=True, emergency=True,
                    visibility="participants", created_by_email=str(account.email), session=db)
            db.commit()
            committed = True
            return Problem(**{field: getattr(problem, field) for field in Problem.model_fields}).model_dump(mode='json')
    except IntegrityError as error:
        constraint = getattr(getattr(error.orig, 'diag', None), 'constraint_name', None)
        if constraint == 'uq_problem_division_code' or str(error.orig) == 'UNIQUE constraint failed: problems.contest_id, problems.division_id, problems.problem_code':
            raise AppError(409, 'problem_archive_code_conflict', '같은 문제 번호가 이미 등록되어 있습니다. 다른 번호로 다시 시도해 주세요.') from error
        log.error('Problem archive database constraint conflict: %s', constraint or 'unknown')
        raise AppError(409, 'problem_archive_data_conflict', '패키지 정보를 저장하는 중 충돌이 발생해 등록을 취소했습니다. 운영자에게 문의해 주세요.') from error
    finally:
        if not committed:
            for key in written:
                try:
                    object_storage.delete(key)
                except Exception:
                    log.exception('Could not remove incomplete problem archive object')
