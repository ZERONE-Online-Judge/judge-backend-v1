from dataclasses import dataclass
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.settings import settings


KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class MailContent:
    subject: str
    body_text: str
    body_html: str


def absolute_url(path: str) -> str:
    base = settings.public_base_url.rstrip("/")
    normalized = path if path.startswith("/") else f"/{path}"
    return f"{base}{normalized}"


def format_korean_datetime(value: datetime) -> str:
    local = value.astimezone(KST)
    return f"{local.year}년 {local.month}월 {local.day}일 {local:%H:%M}"


def _text_html(value: str) -> str:
    # Email clients do not consistently support white-space: pre-wrap.
    return escape(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render_branded_email(
    *,
    title: str,
    preheader: str,
    body: list[str],
    button_label: str | None = None,
    button_url: str | None = None,
    meta: list[tuple[str, str]] | None = None,
    sections: list[tuple[str, str]] | None = None,
    eyebrow: str = "ZOJ 알림",
    verification_code: str | None = None,
    verification_hint: str | None = None,
) -> str:
    body_html = "".join(
        f'<p style="margin:0 0 12px;color:#525b70;font-size:15px;line-height:1.85;word-break:keep-all;overflow-wrap:anywhere">{_text_html(line)}</p>'
        for line in body
        if line
    )
    meta_html = ""
    if meta:
        meta_html = """
        <table width="100%" cellspacing="0" cellpadding="0" style="margin-top:24px;border-collapse:collapse;table-layout:fixed">
        """
        for label, value in meta:
            meta_html += f"""
            <tr>
              <th scope="row" align="left" valign="top" width="80" style="width:80px;border-bottom:1px solid #eceef4;padding:13px 12px 13px 0;color:#737b8f;font-size:12px;line-height:1.7;font-weight:500;word-break:break-word">{_text_html(label)}</th>
              <td valign="top" style="border-bottom:1px solid #eceef4;padding:12px 0;color:#252d43;font-size:14px;line-height:1.7;font-weight:600;word-break:break-word;overflow-wrap:anywhere">{_text_html(value)}</td>
            </tr>
            """
        meta_html += "</table>"
    section_html = ""
    if sections:
        for label, value in sections:
            if not value.strip():
                continue
            section_html += f"""
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:24px;border:1px solid #e8e6f1;border-radius:12px;border-spacing:0;table-layout:fixed">
              <tr><td bgcolor="#f5f3fc" style="padding:12px 18px;border-radius:12px 12px 0 0;color:#6751b4;font-size:12px;font-weight:700;line-height:1.6;word-break:break-word">{_text_html(label)}</td></tr>
              <tr><td style="padding:18px;color:#394157;font-size:14px;line-height:1.9;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere">{_text_html(value.strip())}</td></tr>
            </table>
            """
    verification_html = ""
    if verification_code is not None:
        verification_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:24px;border:1px solid #e5def8;border-radius:16px;table-layout:fixed">
          <tr><td align="center" bgcolor="#f6f3ff" style="padding:26px 12px;border-radius:16px">
            <p style="margin:0 0 12px;color:#78649e;font-size:12px;font-weight:700;line-height:1.5">로그인 인증번호</p>
            <p class="verification-code" dir="ltr" style="margin:0;color:#35285e;font-family:'Courier New',monospace;font-size:36px;font-weight:700;line-height:1.3;letter-spacing:6px;text-indent:6px;white-space:nowrap">{escape(verification_code)}</p>
            <p style="margin:16px 0 0;color:#746788;font-size:12px;line-height:1.7">{escape(verification_hint or '')}</p>
          </td></tr>
        </table>
        <p style="margin:22px 0 0;color:#737b8f;font-size:12px;line-height:1.8">직접 요청하지 않은 메일이라면 무시하셔도 됩니다.<br>인증번호는 다른 사람에게 알려주지 마세요.</p>
        """
    button_html = ""
    if button_label and button_url:
        button_html = f"""
        <table role="presentation" cellspacing="0" cellpadding="0" style="margin-top:28px;border-spacing:0">
          <tr><td align="center" bgcolor="#6d54d8" style="border-radius:10px;mso-padding-alt:15px 24px">
            <a href="{escape(button_url)}" style="display:inline-block;border:1px solid #6d54d8;border-radius:10px;padding:14px 23px;color:#ffffff;font-size:14px;font-weight:700;line-height:1.5;text-decoration:none;mso-padding-alt:0">{escape(button_label)}&nbsp; &#8599;</a>
          </td></tr>
        </table>
        <p style="margin:18px 0 0;color:#858b9a;font-size:11px;line-height:1.8">버튼이 열리지 않으면 아래 주소를 이용해 주세요.<br>
          <a href="{escape(button_url)}" style="color:#7a7099;text-decoration:underline;word-break:break-all;overflow-wrap:anywhere">{escape(button_url)}</a>
        </p>
        """
    home_url = escape(absolute_url("/"))
    preview_spacer = "&#8204;&nbsp;" * 64
    return f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
    <title>{escape(title)}</title>
    <style>
      body, table, td, a {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
      table, td {{ mso-table-lspace:0; mso-table-rspace:0; }}
      a[x-apple-data-detectors] {{ color:inherit!important; text-decoration:none!important; }}
      @media screen and (min-width:481px) {{
        .verification-code {{ font-size:44px!important; letter-spacing:8px!important; text-indent:8px!important; }}
      }}
      @media screen and (max-width:480px) {{
        .mail-outer {{ padding:20px 12px!important; }}
        .mail-brand {{ padding:0 4px 18px!important; }}
        .mail-hero {{ padding:28px 22px!important; }}
        .mail-title {{ font-size:25px!important; }}
        .mail-content {{ padding:26px 22px!important; }}
        .verification-code {{ font-size:34px!important; letter-spacing:6px!important; text-indent:6px!important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#f1f2f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif">
    <div aria-hidden="true" style="display:none!important;visibility:hidden;opacity:0;color:transparent;font-size:1px;line-height:1px;max-height:0;max-width:0;overflow:hidden;mso-hide:all">{escape(preheader)}{preview_spacer}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#f1f2f7" style="border-collapse:collapse;table-layout:fixed">
      <tr>
        <td class="mail-outer" align="center" style="padding:36px 16px">
          <!--[if mso]><table role="presentation" width="600" cellspacing="0" cellpadding="0"><tr><td><![endif]-->
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;border-spacing:0;table-layout:fixed">
            <tr>
              <td class="mail-brand" style="padding:0 4px 22px">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td width="52" valign="middle"><a href="{home_url}" style="text-decoration:none"><img src="cid:zoj-wordmark" alt="ZOJ" width="38" height="42" style="display:block;border:0;width:38px;height:42px;color:#6d54d8;font-size:14px;font-weight:700"></a></td>
                    <td valign="middle" style="color:#394157;font-size:12px;font-weight:600;line-height:1.6">Zerone Online Judge<br><span style="color:#858b9a;font-size:10px;font-weight:400;letter-spacing:1px">CODE. CHALLENGE. GROW.</span></td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td class="mail-hero" bgcolor="#1c2238" style="padding:32px 36px;border-top:4px solid #b9a5f5;border-radius:18px 18px 0 0">
                <p style="margin:0 0 13px;color:#c5e8ab;font-size:12px;line-height:1.5;font-weight:700;letter-spacing:.5px">{escape(eyebrow)}</p>
                <h1 class="mail-title" style="margin:0;color:#ffffff;font-size:29px;line-height:1.45;letter-spacing:-.8px;font-weight:700;word-break:keep-all;overflow-wrap:anywhere">{escape(title)}</h1>
              </td>
            </tr>
            <tr>
              <td class="mail-content" bgcolor="#ffffff" style="padding:30px 36px 34px;border:1px solid #e5e7ef;border-top:0;border-radius:0 0 18px 18px">
                {body_html}
                {verification_html}
                {meta_html}
                {section_html}
                {button_html}
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:24px 12px 0;color:#858b9a;font-size:11px;line-height:1.9">
                ZOJ에서 보내드리는 안내 메일입니다.<br>
                이 메일은 자동 발송되어 회신을 확인하기 어렵습니다.<br>
                <a href="{home_url}" style="display:inline-block;margin-top:10px;color:#6c6584;font-size:11px;text-decoration:none">Zerone Online Judge &#8599;</a>
              </td>
            </tr>
          </table>
          <!--[if mso]></td></tr></table><![endif]-->
        </td>
      </tr>
    </table>
  </body>
</html>"""


def login_verification_mail(*, code: str, ttl_seconds: int, audience: str = "general") -> MailContent:
    context = {"general": "로그인", "staff": "운영진 로그인", "participant": "참가자 로그인"}.get(audience, "로그인")
    minutes, seconds = divmod(ttl_seconds, 60)
    duration = " ".join(part for part in (f"{minutes}분" if minutes else "", f"{seconds}초" if seconds else "") if part) or "0초"
    return MailContent(
        subject=f"[ZOJ] {context} 인증번호 안내",
        body_text=f"{context} 인증번호는 {code} 입니다. {duration} 안에 입력하세요.\n\n직접 요청하지 않은 메일이라면 무시하셔도 됩니다.\n인증번호는 다른 사람에게 알려주지 마세요.",
        body_html=render_branded_email(
            title="로그인을 마무리해 주세요",
            preheader=f"{context}을 위한 인증번호를 보내드립니다. 요청한 로그인 화면에서 입력해 주세요.",
            eyebrow=context,
            body=["아래 인증번호를 로그인 화면에 입력해 주세요."],
            verification_code=code,
            verification_hint=f"요청 시점부터 {duration} 동안 유효합니다.",
        ),
    )


def operator_assignment_mail(
    *,
    contest_title: str,
    organization_name: str,
    display_name: str,
    role_label: str,
    starts_at: datetime | None,
    console_url: str,
) -> MailContent:
    body = [
        f"{display_name} 님, {contest_title}의 {role_label} 권한이 부여되었습니다.",
        "초대받은 이메일로 로그인해 대회와 담당 업무를 확인해 주세요.",
    ]
    meta = [("대회", contest_title), ("주최", organization_name), ("이름 / 역할", f"{display_name} / {role_label}"), ("시작", f"{format_korean_datetime(starts_at)} KST" if starts_at else "일정 확정 전")]
    return MailContent(
        subject=f"[ZOJ] {contest_title} {role_label} 초대 안내",
        body_text="\n".join([*body, "", *(f"{label}: {value}" for label, value in meta), f"바로가기: {console_url}"]),
        body_html=render_branded_email(
            title="함께 대회를 만들어 주세요",
            preheader=f"{contest_title} · {role_label} 초대 안내",
            eyebrow="대회 운영진 초대",
            body=body,
            meta=meta,
            button_label="운영 화면 열기",
            button_url=console_url,
        ),
    )


def render_basic_html(subject: str, body_text: str) -> str:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    return render_branded_email(
        title=subject,
        preheader=lines[0] if lines else subject,
        body=lines or [subject],
    )


def labeled_text_section(label: str, value: str) -> list[str]:
    return [f"{label}:", value.strip()]


def participant_invite_mail(*, contest_title: str, organization_name: str, team_name: str, division_name: str, contest_url: str) -> MailContent:
    subject = f"[ZOJ] {contest_title} 대회에 초대되었습니다"
    body = [
        f"{team_name} 팀의 대회 참가 등록이 완료되었습니다.",
        "등록된 이메일로 로그인해 대회 문제와 공지, 제출 현황을 확인해 보세요.",
    ]
    meta = [
        ("대회", contest_title),
        ("주최", organization_name),
        ("팀", team_name),
        ("유형", division_name),
    ]
    text = "\n".join([*body, "", *(f"{label}: {value}" for label, value in meta), f"바로가기: {contest_url}"])
    return MailContent(
        subject=subject,
        body_text=text,
        body_html=render_branded_email(
            title="대회에 초대되었습니다",
            eyebrow="대회 참가 안내",
            preheader=f"{contest_title} 참가팀 등록 안내",
            body=body,
            meta=meta,
            button_label="대회 페이지 열기",
            button_url=contest_url,
        ),
    )


def contest_reminder_mail(*, contest_title: str, organization_name: str, team_name: str, division_name: str, starts_at: datetime, remaining_label: str, contest_url: str) -> MailContent:
    subject = f"[ZOJ] {contest_title} 시작 {remaining_label} 전 안내"
    starts_at_text = format_korean_datetime(starts_at)
    body = [
        f"{contest_title} 시작이 {remaining_label} 남았습니다.",
        "대회 시작 전에 로그인 상태와 참가 유형을 확인해 주세요.",
    ]
    meta = [
        ("대회", contest_title),
        ("주최", organization_name),
        ("시작", f"{starts_at_text} KST"),
        ("팀", team_name),
        ("유형", division_name),
    ]
    text = "\n".join([*body, "", f"시작: {starts_at_text} KST", f"팀: {team_name}", f"유형: {division_name}", f"바로가기: {contest_url}"])
    return MailContent(
        subject=subject,
        body_text=text,
        body_html=render_branded_email(
            title=f"대회 시작 {remaining_label} 전입니다",
            eyebrow="대회 시작 알림",
            preheader=f"{contest_title} 시작 전 안내",
            body=body,
            meta=meta,
            button_label="대회 페이지 열기",
            button_url=contest_url,
        ),
    )


def contest_notice_mail(
    *,
    contest_title: str,
    organization_name: str,
    notice_title: str,
    notice_body: str,
    notice_url: str,
    pinned: bool,
    emergency: bool,
) -> MailContent:
    subject = f"[ZOJ] {contest_title} 공지: {notice_title}"
    labels = []
    if pinned:
        labels.append("고정")
    if emergency:
        labels.append("긴급")
    notice_type = " · ".join(labels) if labels else "공지"
    body = [
        f"{contest_title}에 새 공지가 등록되었습니다.",
    ]
    meta = [
        ("대회", contest_title),
        ("주최", organization_name),
        ("구분", notice_type),
        ("공지 제목", notice_title),
    ]
    text = "\n".join(
        [
            *body,
            "",
            f"구분: {notice_type}",
            *labeled_text_section("공지 본문", notice_body),
            f"바로가기: {notice_url}",
        ]
    )
    return MailContent(
        subject=subject,
        body_text=text,
        body_html=render_branded_email(
            title=notice_title,
            eyebrow="긴급 공지" if emergency else "대회 공지",
            preheader=f"{contest_title} 새 공지",
            body=body,
            meta=meta,
            sections=[("공지 본문", notice_body)],
            button_label="공지 확인하기",
            button_url=notice_url,
        ),
    )
