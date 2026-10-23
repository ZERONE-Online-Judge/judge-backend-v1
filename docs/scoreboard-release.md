# Frozen counts and problem-based resolver

`freeze_submission_count_visible` is a contest setting, defaulting to false.
It applies to all release strategies while a public board remains frozen,
including during the contest and an immediate-release undo hold. Enabling it
adds `pending_attempts` to unsolved, unrevealed public cells; it never adds
hidden verdicts or affects public ranking. Pre-freeze solved cells stay solved.
Disabling it omits the field entirely. Public totals contain only public
records. Shared submission lists filter hidden rows before counting/paging;
participants retain their own submission access and staff retain actual results.

Manual partial releases retain frozen records for unpublished teams and rank
only the currently public scores. Public release metadata omits hidden final
ranks and ties. Medals are displayed once the whole division is released.
Both participant scoreboards and presentation endpoints use this projection.

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
