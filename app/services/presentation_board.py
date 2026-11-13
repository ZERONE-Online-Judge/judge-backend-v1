from app.services.store import store
from app.models import now_utc
from app.services.presentation_access import aware
from app.services.errors import not_found
from app.services.problem_order import problem_code_sort_key


def presentation_board(
    contest_id: str,
    *,
    display_only: bool = False,
    division_ids: list[str] | None = None
) -> dict:
    contest = store.contests.get(contest_id)
    if not contest:
        raise not_found()

    divisions = store.contest_divisions(contest_id)
    available_divisions = [
        {"division_id": division.division_id, "code": division.code, "name": division.name}
        for division in divisions
    ]
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
                "presentation_freeze_alert_minutes",
                "presentation_end_alert_minutes",
            )
        }
        ended = contest.status in {"ended", "finalized", "archived"} or (
            contest.status not in {"draft", "schedule_tbd"}
            and aware(contest.end_at) <= now_utc()
        )
        if not ended and aware(contest.start_at) > now_utc():
            return {"contest": contest_data, "sections": [], "available_divisions": available_divisions}
    sections = []
    for division in divisions:
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
        problems.sort(key=problem_code_sort_key)
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

    return {"contest": contest_data, "sections": sections, "available_divisions": available_divisions}
