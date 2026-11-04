"""Keep problem codes in the same ordinal order on every contest surface."""
from collections.abc import Mapping


PROBLEM_ORDER_POLICY = "problem-code-ordinal-v1"


def problem_code_sort_key(problem) -> tuple[str, str]:
    if isinstance(problem, Mapping):
        return (problem["problem_code"], problem["problem_id"])
    return (problem.problem_code, problem.problem_id)
