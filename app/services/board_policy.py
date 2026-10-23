"""Contest writing policy never changes the visibility of an existing post."""

from app.services.errors import AppError


def normalize_settings(contest):
    if contest.board_question_visibility_policy != "choice":
        contest.board_question_default_visibility = (
            contest.board_question_visibility_policy
        )
    if contest.board_comment_scope_policy != "choice":
        contest.board_comment_default_scope = contest.board_comment_scope_policy


def question_options(contest, visibility=None, comment_scope=None):
    visibility = visibility or contest.board_question_default_visibility
    comment_scope = comment_scope or contest.board_comment_default_scope
    for value, allowed, policy in (
        (visibility, {"public", "private"}, contest.board_question_visibility_policy),
        (comment_scope, {"all", "operators"}, contest.board_comment_scope_policy),
    ):
        if value not in allowed:
            raise AppError(422, "validation_error", "게시판 작성 범위를 확인해 주세요.")
        if policy != "choice" and value != policy:
            raise AppError(
                403,
                "board_policy_restricted",
                "대회 운영자가 허용한 작성 범위를 선택해 주세요. 작성 화면을 다시 열면 최신 설정이 적용됩니다.",
            )
    return visibility, comment_scope


def effective_comment_scope(contest, question):
    policy = contest.board_comment_scope_policy
    return question.comment_scope if policy == "choice" else policy


def require_participant_comment(contest, question):
    if effective_comment_scope(contest, question) == "operators":
        raise AppError(
            403,
            "board_comments_operators_only",
            "이 질문에는 운영진만 댓글을 작성할 수 있습니다.",
        )
