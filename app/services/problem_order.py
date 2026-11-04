"""Keep problem codes in the same natural order on every contest surface."""
from collections.abc import Mapping
from functools import cmp_to_key


PROBLEM_ORDER_POLICY = "problem-code-natural-v1"


def _compare_codes(left: str, right: str) -> int:
    i = j = 0
    while i < len(left) and j < len(right):
        if "0" <= left[i] <= "9" and "0" <= right[j] <= "9":
            end_i, end_j = i + 1, j + 1
            while end_i < len(left) and "0" <= left[end_i] <= "9":
                end_i += 1
            while end_j < len(right) and "0" <= right[end_j] <= "9":
                end_j += 1
            # Compare arbitrary-length numbers without integer conversion or
            # precision limits. Equal numeric runs continue at the next text.
            number_left = left[i:end_i].lstrip("0") or "0"
            number_right = right[j:end_j].lstrip("0") or "0"
            value_left, value_right = (len(number_left), number_left), (len(number_right), number_right)
            if value_left != value_right:
                return -1 if value_left < value_right else 1
            i, j = end_i, end_j
            continue
        if left[i] != right[j]:
            return -1 if left[i] < right[j] else 1
        i += 1
        j += 1
    if i < len(left) or j < len(right):
        return 1 if i < len(left) else -1
    # Leading-zero variants remain deterministic after all natural runs tie.
    return (left > right) - (left < right)


def _compare_problem_keys(left: tuple[str, str], right: tuple[str, str]) -> int:
    code_order = _compare_codes(left[0], right[0])
    return code_order or ((left[1] > right[1]) - (left[1] < right[1]))


_sort_key = cmp_to_key(_compare_problem_keys)


def problem_code_sort_key(problem):
    if isinstance(problem, Mapping):
        return _sort_key((problem["problem_code"], problem["problem_id"]))
    return _sort_key((problem.problem_code, problem.problem_id))
