"""Adding contests or test versions must not add lock-flag round trips."""
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models import now_utc
from app.orm_models import (ContestRow, ContestDivisionRow, ProblemRow, ScoreboardReleaseRow,
    StaffAccountRow, SubmissionRow, TestcaseSetRow as CaseSetRow)
from app.services.errors import AppError
from app.services.store import store
from tests.test_judge_security import database


@contextmanager
def selects(database):
    statements = []
    def observed(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", observed)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def add_contest(database, index):
    cid, did, pid = f"contest-{index}", f"division-{index}", f"problem-{index}"
    start = now_utc() + timedelta(days=1)
    with database() as db:
        db.add(ContestRow(contest_id=cid, title=cid, organization_name="ZOJ", overview="", status="open",
            start_at=start, end_at=start+timedelta(hours=2), freeze_at=start+timedelta(hours=1)))
        db.flush()
        db.add(ContestDivisionRow(division_id=did, contest_id=cid, code="all", name="All"))
        db.flush()
        db.add(ProblemRow(problem_id=pid, contest_id=cid, division_id=did, problem_code="A", title="A",
            statement="", time_limit_ms=1000, memory_limit_mb=256, display_order=1))
        db.commit()
    return cid, did, pid


def add_submission(db, index=0):
    db.add(SubmissionRow(contest_id=f"contest-{index}", division_id=f"division-{index}",
        problem_id=f"problem-{index}", language="cpp17", source_code="int main(){}", status="accepted"))


def test_contest_list_query_budget_is_flat_and_both_locks_remain_correct(database):
    add_contest(database, 0)
    with selects(database) as single_queries:
        single = store.contests
    assert single["contest-0"].scoring_mode_locked is False
    for index in range(1, 10):
        add_contest(database, index)
    with database() as db:
        add_submission(db)
        db.add(ScoreboardReleaseRow(contest_id="contest-1", division_id="division-1", mode="partial"))
        db.commit()
    with selects(database) as ten_queries:
        ten = store.contests
    assert len(ten) == 10
    assert len(ten_queries) <= len(single_queries) + 1
    assert ten["contest-0"].scoring_mode_locked and not ten["contest-0"].scoreboard_release_locked
    assert ten["contest-1"].scoring_mode_locked and ten["contest-1"].scoreboard_release_locked
    assert all(not ten[f"contest-{index}"].scoring_mode_locked for index in range(2, 10))
    with selects(database) as one_id_queries:
        store.contests_by_ids(["contest-0"])
    with selects(database) as ten_id_queries:
        by_id = store.contests_by_ids(list(ten))
    assert len(ten_id_queries) <= len(one_id_queries) + 1
    assert by_id["contest-1"].scoreboard_release_locked
    with selects(database) as single_get_queries:
        found = store.get_contest("contest-0")
    assert len(single_get_queries) == 1 and found.scoring_mode_locked


def test_master_login_profile_does_not_query_lock_flags_per_contest(database):
    add_contest(database, 0)
    with database() as db:
        db.add(StaffAccountRow(email="master@example.com", display_name="Master", is_service_master=True))
        db.commit()
    with selects(database) as single_queries, database() as db:
        single = store._general_profile(db, "master@example.com")
    assert len(single["operator_contests"]) == 1
    for index in range(1, 10):
        add_contest(database, index)
    with database() as db:
        add_submission(db)
        db.commit()
    with selects(database) as ten_queries, database() as db:
        ten = store._general_profile(db, "master@example.com")
    assert len(ten["operator_contests"]) == 10
    assert len(ten_queries) <= len(single_queries) + 1
    flags = {item["contest"]["contest_id"]: item["contest"]["scoring_mode_locked"] for item in ten["operator_contests"]}
    assert flags["contest-0"] and all(not flags[f"contest-{index}"] for index in range(1, 10))


def test_testset_inventory_batches_locks_and_mutation_responses_remain_current(database):
    cid, did, pid = add_contest(database, 0)
    active = store.create_testcase_set(cid, pid, False)
    with selects(database) as one_queries:
        first = store.testcase_sets
    assert first[active.testcase_set_id].subtasks_locked is False
    for _ in range(9):
        store.create_testcase_set(cid, pid, False)
    with database() as db:
        add_submission(db)
        db.commit()
    with selects(database) as ten_queries:
        ten = store.testcase_sets
    assert len(ten) == 10 and all(item.subtasks_locked for item in ten.values())
    assert len(ten_queries) <= len(one_queries) + 1
    assert all(item["subtasks_locked"] for item in store.testcase_sets_for_problem(cid, pid))
    created = store.create_testcase_set(cid, pid, False)
    assert created.subtasks_locked
    assert store.update_testcase_set(cid, pid, created.testcase_set_id, is_active=True).subtasks_locked
    assert store.delete_testcase_set(cid, pid, active.testcase_set_id).subtasks_locked


def test_lock_projection_is_fresh_after_write_and_rollback_in_same_session(database):
    add_contest(database, 0)
    with database() as db:
        db.add(StaffAccountRow(email="master@example.com", display_name="Master", is_service_master=True))
        db.commit()
        assert not store._general_profile(db, "master@example.com")["operator_contests"][0]["contest"]["scoring_mode_locked"]
        add_submission(db)
        db.flush()
        assert store._general_profile(db, "master@example.com")["operator_contests"][0]["contest"]["scoring_mode_locked"]
        db.rollback()
        assert not store._general_profile(db, "master@example.com")["operator_contests"][0]["contest"]["scoring_mode_locked"]
    assert not store.get_contest("contest-0").scoring_mode_locked
    with database() as db:
        add_submission(db)
        db.commit()
    # A DTO read earlier in a request cannot authorize a now-invalid mutation.
    with pytest.raises(AppError) as error:
        store.update_contest_settings("contest-0", scoring_mode="ioi")
    assert error.value.code == "scoring_mode_locked"
