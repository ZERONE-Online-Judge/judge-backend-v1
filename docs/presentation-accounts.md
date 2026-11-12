# Presentation-only accounts

The scoreboard management page can issue one temporary presentation credential per contest. The address uses eight random, case-insensitive characters and the reserved `@score.zoj.kr` suffix. It is valid for seven days. Reissuing changes the address and invalidates every existing display session; revoking removes the credential immediately. Several browsers or devices may log in and display the scoreboard simultaneously. A new display login does not replace another display's session. General, staff and participant session policies are unchanged.

These are not staff, general, or participant accounts. They do not appear in those lists, participant counts, or standings. Treat the address as the login credential: only scoreboard managers and contest masters can read or issue it. No real mail is sent, and ordinary OTP endpoints reject the reserved suffix.

## API

- `GET /api/operator/contests/{id}/scoreboard/presentation-account`: current credential and expiry, or null.
- `PUT` on the same path: create or replace the credential; requires `contest.scoreboard.manage`.
- `DELETE` on the same path: revoke the credential and all its sessions.
- `POST /api/auth/presentation/login` with `{ "email": "…@score.zoj.kr" }`: issue an isolated `presentation` token. No OTP or general/staff session is issued.
- `GET /api/presentation/contests/{id}/scoreboard`: validate the signed token type, contest, credential generation and expiry, then return only the presentation data. Before the start, only countdown and division-selection metadata is returned. Frozen and unrevealed results use the same public scoreboard projection as the operator presentation.

Presentation responses include `available_divisions` with the contest's `division_id`, `code` and `name` values. Repeated `division_id` query parameters select which sections are computed and returned; the available list still includes all divisions of that contest so a display can change its selection without querying unwanted scoreboards. No division data from other contests is returned. Omitting the filter returns all sections.

Credentials and presentation responses use `Cache-Control: no-store`. Login attempts use an atomic database counter (20 attempts per minute per client, shared across workers). The deployed reverse proxy must continue to overwrite `X-Real-IP`; clients cannot supply their own trusted address through the proxy. Do not log the alias as an ordinary email because knowing it is enough to log in.

Migration `0030_presentation_accounts` adds two tables without modifying existing identities. SQLite startup also creates them through the shared ORM metadata. The frontend stores the display session in its own tab's session storage, does not use general-token fallback or refresh, and routes the tab only to its contest presentation. Expiry/revocation is checked on every scoreboard poll.

The presentation remains an output-only screen: display preferences do not change contest configuration or results. All account and freeze controls stay on the operator scoreboard page. Auto/live/frozen changes require an explanation confirmation followed by a separately focused final warning; cancelling either step sends no settings request.
