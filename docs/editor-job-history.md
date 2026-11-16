# 서비스 관리자에서 Editor 작업 내역 조회

채점 관리의 **작업 내역 (Editor)** 는 Editor에 영속 저장된 실행 요청을
조회합니다. 전체 검증, 단일 검증, 스트레스 테스트, 패키지, 테스트 생성,
Validator·Checker 검증, 지문 제작의 상태·진행률·판정과 상세 결과를
확인합니다. 검색은 문제, 요청자와 실행 이름에 적용합니다.

ZOJ의 `editor_execution_tasks` 는 코드 실행 단위의 하위 작업이며 완료
48시간 뒤 정리됩니다. 이 화면은 해당 큐를 실행 기록으로 사용하지 않고
Editor의 `jobs` 를 읽습니다. 작업 재실행, 취소, 코드·지문·테스트 파일
조회, 결과 반영과 패키지 다운로드 권한은 추가하지 않습니다.

## 공개 API

두 경로 모두 ZOJ의 현재 서비스 마스터 로그인 권한을 확인합니다.
일반 운영자나 참가자는 조회할 수 없습니다. 응답은 `Cache-Control:
no-store` 이며 기존 `{data, request_id}` 응답 형식을 사용합니다.

- `GET /api/admin/judge/editor-jobs`
- `GET /api/admin/judge/editor-jobs/{job_id}`

목록 쿼리:

| 필드 | 기본값 | 범위 |
| --- | --- | --- |
| `page` | `1` | 1~1,000,000 |
| `page_size` | `20` | 1~100 |
| `q` | 빈 문자열 | 최대 200자 |
| `kind` | `all` | `prepare`, `verify`, `validator-tests`, `checker-tests`, `stress`, `package`, `render` |
| `category` | `all` | 위 종류와 `verify-single`, `prepare-full`, `prepare-inputs`, `prepare-validate`, `prepare-answers` |
| `status` | `all` | `queued`, `running`, `completed`, `failed`, `cancelled` |

종류·분류·상태는 `all` 또는 생략을 지원합니다. 목록 `data` 는
`{items, total, page, page_size, pages, summary}` 이고 상세 `data` 는 단일
작업입니다. `summary` 는 조회 범위의 전체 상태별 건수를 제공합니다.
각 작업은 `id`, `problem`, `requester`, `name`, `kind`, `category`, `status`,
`outcome`, `revision`, `workspace_version`, `created_at`, `started_at`,
`finished_at`, `result` 를 제공합니다. 시간은 UTC Unix 초입니다.
`outcome` 은 실행 종료와 검증의 실제 성공 여부를 구분하는 기존 Editor
판정 형식을 유지합니다. 목록 `result` 는 `summary`, `progress` 만
포함하고 상세에는 민감 자료를 제외한 결과와 안전한 `options` 를 제공합니다.
상세의 `result_redacted` 는 민감 자료 제외를, `result_truncated` 는 결과
크기 제한에 따른 일부 생략 여부를 표시합니다.

Editor 연결이 설정되지 않으면 503, 과도한 요청은 503, 연결·프로토콜
오류는 502, 조회 시간 초과는 504, 없는 상세 작업은 404입니다. Editor의
인증 실패를 브라우저의 로그인 만료로 전달하지 않으며 업스트림 오류
본문이나 인증 정보를 응답에 포함하지 않습니다.

## 서버 연결과 배포

```dotenv
EDITOR_API_URL=https://editor.zoj.kr/api
EDITOR_BRIDGE_SECRET=<Editor의 EDITOR_ZOJ_BRIDGE_SECRET과 같은 키>
EDITOR_CA_FILE=
```

서버만 위 설정을 사용합니다. 브라우저 쿠키·Bearer 토큰은 Editor에
전달하지 않습니다. 고정 URL만 요청하며 운영에서는 TLS·인증서 검증을
필수로 하고 HTTP 리다이렉트는 따라가지 않습니다. 연결 제한은 3초,
응답 대기 제한은 15초, 요청 전체 제한은 20초, 응답 본문 상한은 16MiB입니다.

내부 읽기 경로는 `GET /api/internal/admin/jobs` 와 `/{job_id}` 입니다.
기존 `X-Editor-*` 헤더와 공유 키를 쓰되 HMAC 목적은
`zoj-editor-admin-history-v1` 로 분리합니다. 서명 대상은 다음 줄을
개행으로 결합한 문자열입니다.

```text
zoj-editor-admin-history-v1
GET
<실제로 전송한 ASCII 경로와 쿼리 문자열>
<UTC Unix 초>
<32자리 소문자 16진수 nonce>
<소문자 이메일의 padding 없는 base64url>
<빈 본문의 SHA-256>
```

Editor는 90초 이내의 서명, 정확한 경로·쿼리와 빈 본문을 검증하고 기존
nonce 테이블을 사용해 재전송을 차단합니다. 이 키는 브라우저에 노출하지
않습니다. 기존 게시·공유 인증용 서명과 서로 교환해 사용할 수 없습니다.

Editor 내부 조회 API를 먼저 배포하고 ZOJ API, ZOJ 프런트엔드를 적용합니다.
DB 마이그레이션과 채점 에이전트 변경은 없습니다. Editor API 배포는 기존
블루그린 절차와 작업 워커의 drain 정책을 유지합니다. 조회 기능을 위해
실행 중인 작업이나 채점기를 재시작하지 않습니다.
