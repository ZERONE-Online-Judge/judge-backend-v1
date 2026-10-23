from app.services.store import store
from app.models import now_utc
from app.services.presentation_access import aware
from app.services.errors import not_found


def presentation_board(
    contest_id: str,
    *,
    display_only: bool = False,
    division_ids: list[str] | None = None
) -> dict:
    contest = store.contests.get(contest_id)
    if not contest:
        raise not_found()

    contest_data = contest.model_dump(mode="json")
    if display_only:
        contest_data = {
            key: contest_data[key]
            for key in (
                "contest_id",
                "title",
                "status",
                "start_at",
                "end_at",
                "freeze_at",
                "scoreboard_freeze_mode",
                "scoreboard_release_mode",
            )
        }
        ended = contest.status in {"ended", "finalized", "archived"} or (
            contest.status not in {"draft", "schedule_tbd"}
            and aware(contest.end_at) <= now_utc()
        )
        if not ended and aware(contest.start_at) > now_utc():
            return {"contest": contest_data, "sections": []}
    sections = []
    for division in store.contest_divisions(contest_id):
        if division_ids is not None and division.division_id not in division_ids:
            continue
        board = store.scoreboard_rows(
            contest_id, division.division_id, public_view=True
        )
        if not board:
            continue
        problems = [
            problem
            for problem in store.problems.values()
            if problem.contest_id == contest_id
            and problem.division_id == division.division_id
        ]
        problems.sort(
            key=lambda item: (
                item.display_order,
                item.problem_code,
                item.title,
                item.problem_id,
            )
        )
        sections.append(
            {
                "division": division.model_dump(mode="json"),
                "frozen": bool(board["frozen"]),
                "problems": [
                    {
                        key: getattr(problem, key)
                        for key in (
                            "problem_id",
                            "contest_id",
                            "division_id",
                            "problem_code",
                            "title",
                            "display_order",
                        )
                    }
                    for problem in problems
                ],
                "rows": board["rows"],
                "release": board.get("release"),
            }
        )

    return {"contest": contest_data, "sections": sections}
