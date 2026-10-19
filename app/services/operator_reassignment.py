"""Move one contest assignment without renaming or merging login identities."""
import json

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.orm_models import ContestRow, IdentityAccountRow, ParticipantPreviewSessionRow, StaffAccountRow
from app.services.contest_roles import permissions_for_roles
from app.services.errors import AppError


def replace_account(store, contest_id, source_email, target_email, display_name, roles, actor):
    from app.services.store import _staff

    if not actor:
        raise AppError(403, "permission_denied", "운영자 관리 권한이 필요합니다.")
    source_email, target_email = source_email.strip().lower(), target_email.strip().lower()
    if source_email == target_email:
        raise AppError(422, "operator_account_unchanged", "권한을 넘길 다른 계정의 이메일을 입력해 주세요.")
    if not display_name.strip():
        raise AppError(422, "validation_error", "이름을 입력해 주세요.")
    if "owner" in roles:
        raise AppError(409, "contest_owner_immutable", "대회 총괄은 별도의 총괄 위임 기능을 사용해 주세요.")
    permissions = permissions_for_roles(roles)
    try:
        with store._session() as db:
            contest = db.scalar(select(ContestRow).where(ContestRow.contest_id == contest_id).with_for_update())
            if not contest:
                raise AppError(404, "not_found", "대회를 찾을 수 없습니다.")
            accounts = db.scalars(select(StaffAccountRow).where(
                func.lower(StaffAccountRow.email).in_([source_email, target_email])
            ).order_by(StaffAccountRow.staff_account_id).with_for_update()).all()
            source = next((row for row in accounts if row.email.lower() == source_email), None)
            target = next((row for row in accounts if row.email.lower() == target_email), None)
            if not source or not json.loads(source.contest_scopes or "{}").get(contest_id):
                raise AppError(404, "not_found", "기존 운영자를 찾을 수 없습니다.")
            if source.is_service_master or (target and target.is_service_master):
                raise AppError(409, "service_master_operator_immutable", "서비스 마스터는 운영자 계정 변경 대상으로 선택할 수 없습니다.")
            if contest.owner_staff_account_id == source.staff_account_id or contest_id in json.loads(source.protected_master_contests or "[]"):
                raise AppError(409, "contest_owner_immutable", "대회 총괄은 별도의 총괄 위임 기능을 사용해 주세요.")
            store._check_operator_mutation(db, contest, source, roles, actor)
            if target and (json.loads(target.contest_scopes or "{}").get(contest_id) or json.loads(target.contest_roles or "{}").get(contest_id)):
                raise AppError(409, "operator_account_already_assigned", "대상 계정은 이미 이 대회의 운영자입니다. 해당 운영자의 권한을 수정한 뒤 기존 운영자를 제거해 주세요.")
            if store._contest_participant_email_conflicts(db, contest_id, [target_email]):
                raise AppError(422, "operator_account_participant", "같은 대회에 참가 중인 계정에는 운영자 권한을 넘길 수 없습니다.")
            if not target:
                identity = db.scalar(select(IdentityAccountRow).where(func.lower(IdentityAccountRow.email) == target_email))
                target = StaffAccountRow(email=target_email, display_name=(identity.display_name if identity and identity.display_name else display_name.strip()),
                    is_service_master=False, permissions="", contest_scopes="{}", contest_roles="{}", protected_master_contests="[]")
                db.add(target)
                db.flush()
            # Each identity keeps its email, passwords, sessions, other contests
            # and authorship. Only this contest's assignment changes hands.
            for row, granted in ((source, False), (target, True)):
                scopes = json.loads(row.contest_scopes or "{}")
                selections = json.loads(row.contest_roles or "{}")
                if granted:
                    scopes[contest_id], selections[contest_id] = permissions, roles
                else:
                    scopes.pop(contest_id, None)
                    selections.pop(contest_id, None)
                row.contest_scopes, row.contest_roles = json.dumps(scopes), json.dumps(selections)
            db.execute(delete(ParticipantPreviewSessionRow).where(
                ParticipantPreviewSessionRow.staff_account_id.in_([source.staff_account_id, target.staff_account_id]),
                ParticipantPreviewSessionRow.contest_id == contest_id))
            db.commit()
            return _staff(target)
    except IntegrityError as error:
        raise AppError(409, "operator_account_changed", "대상 계정의 상태가 변경되었습니다. 운영자 목록을 새로고침한 뒤 다시 시도해 주세요.") from error
