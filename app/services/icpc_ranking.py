"""ICPC standings: solved count, penalty, then the last accepted run."""
from datetime import datetime, timezone


ICPC_RANKING_POLICY = "icpc-last-accepted-v1"


def rank_key(row: dict) -> tuple:
    last_solved = row.get("last_solved_at")
    if isinstance(last_solved, str):
        last_solved = datetime.fromisoformat(last_solved)
    if last_solved is None:
        last_solved = datetime.max.replace(tzinfo=timezone.utc)
    elif last_solved.tzinfo is None:
        last_solved = last_solved.replace(tzinfo=timezone.utc)
    return (-row["solved"], row["penalty"], last_solved)


def rank_rows(rows: list[dict]) -> None:
    # Names stabilize display only; they never break an official tie.
    rows.sort(key=lambda row: (*rank_key(row), row["team_name"]))
    previous_key, rank = None, 0
    for index, row in enumerate(rows, start=1):
        key = rank_key(row)
        if key != previous_key:
            previous_key, rank = key, index
        row["rank"] = rank
