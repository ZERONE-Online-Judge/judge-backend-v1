# Frozen counts and problem-based resolver

`freeze_submission_count_visible` is a contest setting, defaulting to false.
It applies to all release strategies while a public board remains frozen,
including during the contest and an immediate-release undo hold. Enabling it
adds `pending_attempts` to unsolved, unrevealed public cells; it never adds
hidden verdicts or affects public ranking. Pre-freeze solved cells stay solved.
Disabling it omits the field entirely. Public totals contain only public
records. Shared submission lists filter hidden rows before counting/paging;
participants retain their own submission access and staff retain actual results.

Manual rank announcements publish the selected final-rank group atomically,
including all teams tied at that rank. During a partial release, announced rows
come first in final-rank order and carry `rank_is_final: true`; their `rank`
stays at the captured final position as other ranks are announced. Unpublished
rows follow in their original frozen order with their frozen `rank` and
`rank_is_final: false`. Clients present these as separate confirmed-rank and
unannounced frozen-record groups, never as one mixed-score ranking. Hidden final
positions, ties, scores and ordering are not included in the public projection.
`is_finalized` still means that the team's result processing is complete; it
does not announce a final rank. The rank-confirmation field is absent before
announcements, after full release, and in the resolver/immediate strategies.
Undo and resume retain the same snapshot and announced-rank groups. Medals are
displayed once the whole division is released. Participant scoreboards, basic
staff views and presentation endpoints use this projection; staff with actual
scoreboard permission retain the separate internal results.

Resolver `step`, `total_steps`, and `pending_count` count team/problem cells,
not submissions. A next action selects the last displayed team with pending
results, then its first pending problem in problem display order. The action
processes that cell chronologically through its first accepted submission or
all failures. Later submissions on a solved cell require no further action.
Compilation and infrastructure failures do not add wrong-attempt penalties.

Each action commits the whole cell and new ranking in one transaction. The
contest row lock and expected revision/step reject concurrent stale actions.
An undo delta stores the previous team row and every removed event with its
original position, so one undo restores the whole cell, counts and ranking.
All-release undo stores the previous resolver state. Snapshots survive rejudging,
reloads and resume. Legacy progress is normalized without replaying verdicts.
SSE signals use only committed revisions and the count-policy field; clients
refetch the authorized public projection.

Migration 0057 adds the boolean with a false default for existing contests.

## Anonymous access during a contest

`scoreboard_public_during_contest` defaults to false and independently
controls anonymous scoreboard access while a public contest is running.
It does not open a private contest or expose results before the start.
After the contest ends, `scoreboard_access_after_end` alone controls access.
Both the default-division and explicit-division endpoints, including their
wait variants, authorize every request before serving a cached response.
Anonymous views use the same public freeze and release projection as the
participant scoreboard; operator internal results remain separate.
