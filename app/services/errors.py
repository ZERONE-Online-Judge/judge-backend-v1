class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class TestcaseResultsInvalid(AppError):
    """The lease is valid, but the reported testcase evidence is invalid."""

    def __init__(self, reason: str):
        super().__init__(422, "testcase_results_invalid",
            "채점 결과 증거를 확인하지 못했습니다. 관리자에게 문의해 주세요.",
            {"reason": reason})


def not_found(message: str = "Resource not found.") -> AppError:
    return AppError(404, "not_found", message)


def permission_denied(message: str = "You do not have permission to perform this action.") -> AppError:
    return AppError(403, "permission_denied", message)


def authentication_required(message: str = "Authentication is required.") -> AppError:
    return AppError(401, "authentication_required", message)


def scope_denied(message: str = "You do not have permission for this contest.") -> AppError:
    return AppError(403, "scope_denied", message)


def invalid_state(message: str) -> AppError:
    return AppError(409, "invalid_state_transition", message)
