# Production Compose Runtime

## Division ranking release

Final ranking releases require migration `0023_scoreboard_releases` before deploying the corresponding frontend.
After a contest ends, operators can start an individual release or reveal all ranks for a single division.
The release stores a fixed ranking snapshot after that division's participant judging finishes. Later rejudging does not change this snapshot.
Revealing a rank reveals all teams tied at that rank. Other divisions retain their existing state.

- `GET /api/operator/contests/{contest_id}/divisions/{division_id}/scoreboard/release`: release state and rank choices (no hidden team identities).
- `POST` to the same endpoint with `{"action":"start"}`, `{"action":"rank","rank":2}`, or `{"action":"all"}`.
- Public scoreboards and the presentation use the same persisted state. Unrevealed rows omit team identities and results on the server.
- The operator internal scoreboard remains live for review. Existing access policies still determine who can view the public scoreboard.

## 1. Generate Runtime Env Files

```bash
sh backend_v1/deploy/init-env.sh
```

This creates ignored runtime files from `backend_v1/deploy/env/*.env.example`.
Edit every `change-me` value before running production.

Required production values:

- `backend_v1/deploy/env/backend.env`: domain, CORS, PostgreSQL URL, MinIO credentials, SMTP credentials, bootstrap service master account
- `backend_v1/deploy/env/db.env`: PostgreSQL database/user/password
- `backend_v1/deploy/env/minio.env`: MinIO root user/password

## 2. Build Frontend

```bash
npm --prefix demo_frontend run build
```

The production frontend uses relative `/api`, which Nginx proxies to the backend container.
Nginx is the only public entrypoint for the backend stack.

Optional test frontend domain:

- `zoj.kr` is served from `${FRONTEND_DIST_PATH:-../../demo_frontend/dist}`.
- `test.judge.zerone01.kr` is served from `${TEST_FRONTEND_DIST_PATH:-../../demo_frontend/dist}`.
- Both domains proxy `/api`, `/minio`, and `/minio-console` to the same backend stack.

If you want a different build for the test domain, build it into a separate directory and pass it to compose:

```bash
TEST_FRONTEND_DIST_PATH=/srv/zoj/test-frontend-dist \
docker compose -f backend_v1/deploy/compose.backend.yaml up -d --force-recreate nginx
```

If a host-level Nginx terminates TLS in front of this compose stack, proxy both hostnames to the compose Nginx port while preserving `Host`:

```nginx
server {
  listen 443 ssl http2;
  server_name zoj.kr www.zoj.kr judge.zerone01.kr test.judge.zerone01.kr;

  location / {
    proxy_pass http://127.0.0.1:6001;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

Also point DNS for `test.judge.zerone01.kr` to the same server and issue a certificate that covers the test hostname.

## 3. Start Backend Stack

```bash
sh backend_v1/deploy/init-judge-tls.sh
docker compose -f backend_v1/deploy/compose.backend.yaml up -d --build
```

Services:

- `nginx`: frontend static files, `/api`, `/minio`, `/minio-console` reverse proxy
- `api-blue` and `api-green`: FastAPI backend pools for blue-green switching, internal compose network only
- `migrate`: Alembic migration job
- `postgres`: production DB
- `redis`: reserved runtime queue/cache dependency
- `minio` and `minio-init`: object storage and bucket creation, internal compose network only
- `mail-worker`: SMTP or Resend mail queue consumer
- `verification-ai-worker`: shared GPT analysis of verification verdict mismatches; configure `OPENAI_API_KEY` in `deploy/env/backend.env`. See [configuration, cache and troubleshooting](../docs/verification-ai.md).
- `notice-worker`: scheduled emergency notice publisher
- `postgres-backup`: legacy optional profile, daily database-only `pg_dump -Fc`

For production, use the [full ZOJ service backup](../docs/service-backups.md).
It captures PostgreSQL, MinIO, Redis, the frontend, runtime images and host
configuration together on the dedicated disk, with isolated restore drills.
The legacy profile below covers only the database and is not a replacement:

```bash
docker compose -f backend_v1/deploy/compose.backend.yaml --profile backup up -d postgres-backup
```

## 4. Blue-Green API Deploy

Nginx proxies `/api` to `api_backend`, which is defined by `deploy/nginx/api-upstream.conf`.
The active pool is either `api-blue:8000` or `api-green:8000`.
Traffic switches by rewriting the upstream file and reloading Nginx. Compose
recreates the proxy only when its container configuration changes. Both Nginx
configuration directories are mounted so Git file replacements are visible on
subsequent reloads. The web proxy uses `/etc/nginx/zoj/nginx.conf`; the private
judge gateway retains the image's default main configuration.

Check current state:

```bash
cd backend_v1/deploy
./bluegreen.sh status
```

Deploy a new API version to the inactive pool:

```bash
cd backend_v1/deploy
./bluegreen.sh deploy green
```

Pull backend `main`, deploy the inactive API pool, switch traffic, and stop the previous API pool:

```bash
cd backend_v1/deploy
./deploy-main-bluegreen.sh
```

The one-command deploy script only pulls `backend_v1` from `origin main`.
The backend deploy aborts when the backend worktree has uncommitted changes. Override only when intentional:

```bash
ALLOW_DIRTY=1 ./deploy-main-bluegreen.sh
```

If the public health endpoint is not `http://127.0.0.1:6001/api/health`, set it explicitly:

```bash
PUBLIC_HEALTH_URL=https://judge.example.com/api/health ./deploy-main-bluegreen.sh
```

Rollback is only an upstream switch:

```bash
cd backend_v1/deploy
./bluegreen.sh switch blue
```

The deploy command runs in this order:

1. Verify the connection budget for both API colors, workers and migrations.
2. Run Alembic migrations.
3. Start/build the target API pool.
4. Check the target pool with `/api/health`.
5. Rewrite `nginx/api-upstream.conf`.
6. Validate and reload the web proxy with `nginx -c /etc/nginx/zoj/nginx.conf`.
7. Validate and reload the private judge gateway using its default config.

The budget check reads the server's `max_connections` and the resolved Compose
settings. It retains ten spare database connections and stops deployment if the
configured pools could exhaust the database during a blue-green overlap. Python
3 is required on the deployment host. Environment values are not printed.

Database migrations must be backward-compatible with the currently active API.
Use expand-and-contract migrations:

1. Add nullable columns/tables/indexes first.
2. Deploy the new API.
3. Switch traffic.
4. Remove old columns or incompatible behavior in a later deploy.

Runtime release metadata:

```bash
RELEASE_VERSION=2026-05-14.1 ./bluegreen.sh deploy green
curl https://judge.example.com/api/health
```

Feature flags live in `env/backend.env`:

```env
FEATURE_SUBMISSION_RUNTIME_METRICS=true
FEATURE_PUBLIC_SCOREBOARD_PENALTY=true
FEATURE_EMERGENCY_NOTICE_AUTO=true
```

## Canonical public domain

The production site is `https://zoj.kr`. The compose Nginx redirects all
`www.zoj.kr` and `judge.zerone01.kr` requests to this origin with HTTP 308,
preserving the path, query string and HTTP method. This includes the API;
there is no legacy judge API exception. All agents must use the new origin
or a private API endpoint before deploying the redirect. Python urllib does
not replay POST requests across 308 redirects.

`test.judge.zerone01.kr` remains a separate test frontend. Localhost/IP requests
remain available for deployment health checks. The outer TLS proxy must
preserve `Host` and maintain DNS and valid certificates for all production
hostnames, including the redirect aliases.

Runtime settings in `deploy/env/backend.env`:

```env
PUBLIC_BASE_URL=https://zoj.kr
CORS_ALLOW_ORIGINS=https://zoj.kr
```

Preserve additional test/local origins if the installation uses them. Recreate
API and background workers through the blue-green deploy to apply env changes.
The public URL controls email links and signed storage URLs. Relative frontend
`/api` requests need no change. Canonical/Open Graph metadata, robots.txt,
sitemap.xml and the development API proxy must also use zoj.kr.

Each judge machine must have runtime `INTERNAL_API_BASE_URL=https://zoj.kr/api`
(private API endpoints can remain). Recreate agents when they have no active
jobs and verify their heartbeats before enabling the redirects. The judge-agent
repository provides `deploy/update-api-domain.sh` for the existing node fleet.
Changing installer defaults alone does not update existing agents.

Validate HTTPS, signed downloads/uploads, CORS, judge heartbeats, and redirect
paths/queries after deploying. Existing browser login state and local code
drafts are origin-specific and do not automatically move to zoj.kr. Accounts
and submissions on the server remain unchanged. Keep token signing keys and
database/storage credentials unchanged during this migration.

## 검색엔진용 공개 문서

운영 Nginx는 `/`, 소개, 대회·공지와 지원 안내 등 페이지 요청을
`/api/public/seo/document`로 전달합니다. API는 읽기 전용으로 마운트한
`/frontend/index.html`의 JS/CSS 진입점을 유지하면서 페이지별 제목, 설명,
canonical, Open Graph, JSON-LD와 공개 본문을 함께 내려줍니다. 브라우저와
검색 로봇에 같은 HTML을 제공하며, React가 실행되면 기존 화면이 렌더링됩니다.

- `PUBLIC_BASE_URL=https://zoj.kr`: 대표 주소. 요청의 Host 헤더로 만들지 않습니다.
- `FRONTEND_HTML_PATH=/frontend/index.html`: API 안의 빌드된 HTML 경로.
- API와 Nginx가 같은 프런트엔드 `dist` 디렉터리를 읽어야 합니다.
- `GOOGLE_SITE_VERIFICATION`, `NAVER_SITE_VERIFICATION`: 선택적인 소유권 확인 값.
  비어 있으면 프런트엔드 HTML에 이미 설정한 확인 태그를 그대로 보존합니다.
- `/sitemap.xml`: 공개 대회와 서비스 공지가 추가·삭제되면 자동 반영됩니다.
  관리자·참가자 전용 화면이나 문제 원문·제출은 포함하지 않습니다.
- `/robots.txt`: 크롤링 경로와 사이트맵을 안내합니다. 로그인·운영자 화면은
  접근 제어와 `noindex`를 사용하며, 비공개 여부를 robots.txt에 의존하지 않습니다.
- 존재하지 않거나 공개되지 않은 대회/공지의 페이지 요청은 HTTP 404입니다.
- 테스트 도메인은 `X-Robots-Tag: noindex, nofollow`를 반환합니다.

Google Search Console 및 네이버 서치어드바이저에서 제출할 사이트맵은
`https://zoj.kr/sitemap.xml`입니다. 제출 이후 수집·색인 상태는 각 도구에서
확인해야 하며, 사이트맵 제출이 검색 순위나 즉시 색인을 보장하지는 않습니다.

Nginx 설정은 `deploy/nginx` 디렉터리 전체를 마운트합니다. 배포 스크립트는
새 API의 상태 확인 후 별도 컨테이너에서 Nginx 설정을 검사하고, 변경된
마운트를 반영한 뒤 리로드합니다. 개별 파일 마운트에서 전환하는 첫 배포에는
Nginx 컨테이너가 한 번 재생성됩니다.

SEO 엔드포인트가 없는 이전 API로 롤백할 때에는 Nginx의 HTML 전달 설정도
함께 이전 버전으로 복구한 뒤 문법 검사와 리로드를 진행해야 합니다.

## Judge credentials and private API

Judge nodes must be provisioned by an administrator before their first connection.
`POST /api/internal/judge/nodes/register` only authenticates an existing, enabled
node; it cannot create credentials. Existing enrolled nodes retain their IDs and
secrets on upgrade. Offline nodes are retained instead of losing their identity.
Review the existing registry before rollout and revoke any unrecognized entry.

Run these commands on the backend host, substituting the active API color:

```bash
cd /home/zoj/zerone-online-judge/backend-v1
color=$(sh deploy/bluegreen.sh active)
docker compose -f deploy/compose.backend.yaml exec "api-$color" python -m app.tools.judge_nodes list
docker compose -f deploy/compose.backend.yaml exec "api-$color" python -m app.tools.judge_nodes provision zoj-judge-agent-06 --slots 10
```

Provisioning and `rotate NAME` prompt for a secret without echoing it. Use a unique,
random secret of at least 32 characters per node and configure the same value as
`JUDGE_NODE_SECRET` on that node. Do not pass secrets in shell arguments or tickets.
An already provisioned name is never overwritten. `revoke NAME` immediately denies
new operations and requeues outstanding work; `rotate NAME` also invalidates
outstanding leases. `enable NAME` re-enables the current credential. The CLI has no
public HTTP equivalent. Nodes removed by older versions must be provisioned once
again; this includes powered-off nodes 6 and 7 if absent from `list`.

All judge operations require an enabled node secret. Only the assigned node can
update a running job with its current, unexpired lease. Final results invalidate
the lease, and progress requests cannot set final verdicts. Concurrent writes are
serialized in PostgreSQL. Approved workers remain trusted to execute the judge
correctly: a stolen worker secret or compromised approved VM still requires
revocation and investigation; this protocol does not attest computation.

The main and test Nginx virtual hosts deny `/api/internal/judge/` entirely.
The private TLS gateway accepts only TCP peers `10.10.10.111` through
`10.10.10.117`. Supplied forwarding headers do not grant access.
Keep backend container port 8000 unexposed, and do not enable broad
`set_real_ip_from` rules on these proxies.

The `judge-gateway` service serves `https://10.10.10.110:6443/api` and signed
`/minio/` downloads with TLS 1.2/1.3, bound only to the LAN address. Its server key
is generated once by `deploy/init-judge-tls.sh` in ignored `deploy/env/judge-tls/`.
Never distribute `server.key`. Install the public `server.crt` into authorized
agent containers over a trusted channel; use the agent repository's
`deploy/use-internal-tls.sh` for the current installation. The rollout verifies
both the server certificate and IP address, then preserves normal Python TLS
verification. Do not use insecure TLS options.

HTTP port 6001 no longer accepts judge credentials, including from agent IPs.
All active nodes must use 6443 before installing this configuration. Returning
offline nodes must also install trusted TLS before reconnecting. Historical
agent rollback scripts that restore HTTP settings require an updated TLS
configuration; HTTP credential transport must not be re-enabled as a fallback.
The deployment reloads both proxies before the old API is stopped.

Certificates expire after one year. Monitor with
`openssl x509 -checkend 2592000 -noout -in deploy/env/judge-tls/server.crt`.
Before renewal, distribute trust for the replacement certificate to all active
and returning nodes, then replace the server key/certificate and reload the
judge gateway. Existing keys are never silently regenerated. The pinned agent
wrapper must be updated for a new server certificate.

Validation:

```bash
DATABASE_URL=sqlite:////tmp/judge-tests.db ENABLE_DEMO_SEED=true ALLOW_EMPTY_OTP=true python -m pytest tests
# Use only a disposable PostgreSQL; concurrency tests create/drop private schemas.
ZOJ_TEST_POSTGRES_URL=postgresql+psycopg://postgres@localhost/judge_test python -m pytest tests/test_judge_concurrency.py
```

Reference behavior: [Nginx address-based access control](https://nginx.org/en/docs/http/ngx_http_access_module.html)
and [Python certificate verification](https://docs.python.org/3/library/ssl.html).
