# 대회 공개 여부

`visibility`와 `visibility_after_end`는 `public` 또는 `private`이며, 기존 대회는 두 값 모두 `public`으로 유지됩니다. 진행 상태(`status`)와 별도입니다.

- `visibility`: 종료 전 목록·소개·참가자 경로의 공개 여부입니다.
- `visibility_after_end`: 종료 시각 또는 종료 상태부터 적용합니다. 공개 대회라도 자료별 열람 범위는 따로 확인합니다.
- 비공개 대회는 현재 유효한 세션의 해당 참가자 또는 배정된 운영자만 목록에서 볼 수 있습니다. 다른 계정과 방문자에게는 직접 링크도 404로 처리합니다.
- 초안·일정 미정·예정(비공개)은 참가자에게도 숨깁니다. 운영자는 운영 페이지에서 관리하며, 기존 일정에 따른 상태 전환은 유지합니다.
- 종료 후 비공개로 저장하면 여섯 자료의 `public` 설정을 `participants`로 바꿉니다. 다시 대회를 공개해도 자료는 자동으로 비로그인 공개되지 않습니다.
- 비공개 대회는 사이트맵·검색 메타데이터·검색용 목록에서 제외합니다. 공개 목록 API는 인증 정보를 받아 응답하고, 캐시는 계정별로 분리하며 `private, no-store`를 사용합니다.

서비스 관리자의 대회 생성 요청과 운영자 설정 PATCH에 두 필드를 사용할 수 있습니다. 운영 중에도 공개 여부를 바꿀 수 있지만, 기존 기본 정보·상태 변경 제한은 유지합니다. 참가자·운영자 데이터나 기존 대회의 공개 정책을 일괄 변경하지 않습니다.

## 대회 삭제

서비스 마스터의 대회 관리 목록에서 `삭제`를 누른 후 대회명을 직접 입력합니다.
진행 중인 대회와 대기·배정·실행 중인 채점 작업이 남아 있는 대회는 삭제할 수
없습니다. API는 `DELETE /api/admin/contests/{contest_id}`이며, 본문에
`{"confirm_title": "삭제할 대회명"}`을 전달합니다. 대회 운영자 권한만으로는
삭제할 수 없습니다.

삭제한 대회는 서비스 마스터·운영자·참가자 목록과 공개 메타데이터에서 제외되고,
기존 주소로 접근해도 찾을 수 없습니다. 해당 대회의 참가자 세션과 미리보기
연결은 해제하며, 알림은 숨기고 대기 중인 대회 메일은 취소합니다. 다른 대회의
권한, 로그인 계정과 로그인 세션은 유지합니다.

삭제는 `contests.deleted_at`을 기록하는 방식입니다. 문제·제출·참가 정보와
운영 기록, 저장 파일을 영구 삭제하지 않습니다. 삭제 이력은 운영 로그에
남으며, 화면에서 복원하는 기능은 제공하지 않습니다. 내부 이력 조회가 필요한
경우에만 ORM 쿼리에 `include_deleted_contests=True` 실행 옵션을 사용합니다.

## Participation totals before the start

`hide_participation_counts_before_start` defaults to `false`, including
existing contests. When enabled, the public contest detail response omits
both `participant_count` and `team_count` before `start_at`, regardless of
whether the caller is anonymous or signed in. At the start instant the
totals become available automatically. Internal operator dashboards and
the participant's own team roster remain available. This setting does not
change contest visibility or resource access.
