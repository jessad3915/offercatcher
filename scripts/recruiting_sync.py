#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(
    os.environ.get(
        "OFFER_RADAR_STATE_PATH",
        str(Path.home() / ".openclaw" / "workspace" / "memory" / "offer-radar-state.json"),
    )
)
REMINDERS_SCRIPT = REPO_ROOT / "scripts" / "apple_reminders_bridge.py"

SEARCH_QUERY_TEMPLATE = "newer_than:{days}d in:inbox"
DEFAULT_DAYS = 5
DEFAULT_MAX = 50
DEFAULT_ACCOUNT = os.environ.get("GOG_ACCOUNT", "")
DEFAULT_LIST = "OpenClaw"
MAIL_TIMEOUT_SECONDS = 120
MAIL_BODY_LIMIT = 12000

IGNORE_SUBJECT_KEYWORDS = (
    "投递成功",
    "收到你的申请",
    "感谢投递",
    "感谢您的应聘",
    "简历完善通知",
    "简历完善",
    "面试反馈问卷",
    "体验调研",
    "问卷",
    "进度通知",
    "邀请反馈",
)

PRIORITY_EVENT_TYPES = {
    "interview": 0,
    "ai_interview": 1,
    "written_exam": 2,
    "assessment": 3,
    "authorization": 4,
}

KNOWN_SENDERS = (
    "tencent.com",
    "campus.tencent.com",
    "people@mail.bytedance.net",
    "careers.bytedance.com",
    "mail.mokahr.com",
    "nowcoder.net",
    "shmail.ibeisen.com",
    "aixuexi.com",
)

COMPANY_ALIASES = (
    ("字节跳动", "字节跳动"),
    ("bytedance", "字节跳动"),
    ("腾讯", "腾讯"),
    ("pdd", "拼多多"),
    ("拼多多", "拼多多"),
    ("trip.com", "携程"),
    ("携程", "携程"),
    ("美图", "美图"),
    ("爱学习", "爱学习教育"),
    ("aixuexi", "爱学习教育"),
    ("高思教育", "爱学习教育"),
)

URL_RE = re.compile(r"https?://[^\s<>\u3000)）]+")


@dataclass
class CandidateMail:
    thread_id: str
    subject: str
    sender: str
    received_at: dt.datetime
    labels: list[str]
    message_count: int
    body: str = ""


@dataclass
class Event:
    state_key: str
    source_ids: list[str]
    source_subjects: list[str]
    company: str
    event_type: str
    title: str
    note: str
    main_due: str
    timing: dict[str, str]
    links: list[dict[str, str]]
    received_at: dt.datetime
    priority: str = "high"
    role: str = ""
    source_sender: str = ""

    def to_state_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "eventType": self.event_type,
            "title": self.title,
            "timing": self.timing,
            "links": self.links,
            "note": self.note,
            "mainReminder": {
                "title": self.title,
                "due": self.main_due,
                "priority": self.priority,
            },
            "source": {
                "threadIds": self.source_ids,
                "subject": self.source_subjects[-1],
                "subjects": self.source_subjects,
                "sender": self.source_sender,
                "lastSeenAt": self.received_at.strftime("%Y-%m-%d %H:%M"),
            },
        }
        if self.role:
            entry["role"] = self.role
        return entry


def run_json(cmd: list[str]) -> Any:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    return json.loads(proc.stdout)


def run_text(cmd: list[str]) -> str:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    return proc.stdout


def applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def discover_mail_account(explicit_account: str) -> str:
    if explicit_account:
        return explicit_account

    script = ['tell application "Mail" to return name of every account']
    accounts = run_text(["osascript", "-e", script[0]]).strip()
    if not accounts:
        return ""

    candidates = [item.strip() for item in accounts.split(",")]
    for account in candidates:
        lowered = account.lower()
        if "gmail" in lowered or "google" in lowered or "谷歌" in account:
            return account
    return candidates[0]


def fetch_mail_body(mail_account: str, subject: str) -> str:
    if not mail_account:
        return ""

    escaped_account = applescript_escape(mail_account)
    escaped_subject = applescript_escape(subject)
    script = [
        f"with timeout of {MAIL_TIMEOUT_SECONDS} seconds",
        'tell application "Mail"',
        (
            f'set hits to (every message of mailbox "INBOX" of account "{escaped_account}" '
            f'whose subject contains "{escaped_subject}")'
        ),
        'if (count of hits) is 0 then return ""',
        "set m to item 1 of hits",
        "repeat with candidateMessage in hits",
        "if (date received of candidateMessage) > (date received of m) then set m to candidateMessage",
        "end repeat",
        "set c to content of m",
        f"if (length of c) > {MAIL_BODY_LIMIT} then return text 1 thru {MAIL_BODY_LIMIT} of c",
        "return c",
        "end tell",
        "end timeout",
    ]
    cmd = ["osascript"]
    for line in script:
        cmd.extend(["-e", line])
    try:
        return run_text(cmd).strip()
    except RuntimeError:
        return ""


def load_candidates(account: str, days: int, max_results: int) -> list[CandidateMail]:
    cmd = ["gog", "gmail", "search"]
    if account:
        cmd.extend(["-a", account])
    cmd.extend(["-j", "--results-only", SEARCH_QUERY_TEMPLATE.format(days=days), "--max", str(max_results)])
    rows = run_json(cmd)

    candidates: list[CandidateMail] = []
    for row in rows:
        received_at = dt.datetime.strptime(row["date"], "%Y-%m-%d %H:%M")
        candidates.append(
            CandidateMail(
                thread_id=row["id"],
                subject=row["subject"],
                sender=row["from"],
                received_at=received_at,
                labels=row.get("labels", []),
                message_count=row.get("messageCount", 1),
            )
        )
    return candidates


def looks_like_receipt(subject: str) -> bool:
    lowered = subject.lower()
    if any(keyword in subject for keyword in IGNORE_SUBJECT_KEYWORDS):
        return True
    if "感谢" in subject and all(token not in subject for token in ("面试", "笔试", "测评", "授权")):
        return True
    if "申请" in subject and all(token not in subject for token in ("面试", "笔试", "测评", "授权")):
        return True
    return "feedback" in lowered and "面试" not in subject


def is_candidate(mail: CandidateMail) -> bool:
    subject = mail.subject
    sender_lower = mail.sender.lower()
    if looks_like_receipt(subject):
        return False
    if any(domain in sender_lower for domain in KNOWN_SENDERS):
        if any(token in subject for token in ("面试", "笔试", "测评", "授权", "assessment", "AI面试")):
            return True
    include_tokens = ("面试邀请", "面试信息有更新", "面试提醒", "AI面试", "笔试", "测评", "授权")
    return any(token in subject for token in include_tokens)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n").replace("\u00a0", " ").replace("\ufeff", "")
    text = text.replace("\u2002", " ").replace("\u2003", " ").replace("\u2009", " ")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def simplify_company(name: str) -> str:
    text = name.strip()
    text = re.sub(r"(有限责任公司|股份有限公司|科技有限公司|集团公司|有限公司|公司)$", "", text)
    text = text.replace("（", "(").replace("）", ")")
    for needle, canonical in COMPANY_ALIASES:
        if needle in text:
            return canonical
    return text


def detect_company(subject: str, sender: str, body: str) -> str:
    body_patterns = (
        re.compile(r"感谢您对\s*([^\n。！!]{2,40}?)(?:有限公司|集团公司|科技有限公司|公司的)?关注"),
    )
    match = body_patterns[0].search(body)
    if match:
        return simplify_company(match.group(1))

    if "aixuexi.com" in body.lower():
        return "爱学习教育"

    head = f"{subject}\n{sender}"
    lowered_head = head.lower()
    for needle, canonical in COMPANY_ALIASES:
        if needle.lower() in lowered_head:
            return canonical

    lowered_body = body.lower()
    for needle, canonical in COMPANY_ALIASES:
        if needle.lower() in lowered_body:
            return canonical

    return "待确认公司"


def detect_event_type(subject: str, body: str) -> str:
    merged = f"{subject}\n{body}"
    if "授权" in merged:
        return "authorization"
    if "AI面试" in merged:
        return "ai_interview"
    if "笔试" in merged:
        return "written_exam"
    if "测评" in merged or "assessment" in merged.lower():
        return "assessment"
    return "interview"


def clean_role(role: str) -> str:
    text = role.strip()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(点击链接进入面试|面试地址|链接：).*", "", text)
    text = text.replace("职位", "")
    text = text.replace("产品研发-", "")
    text = re.sub(r"\s+", "", text)
    if any(token in text for token in ("http", "meeting", "链接", "地址")):
        return ""
    return text


def extract_role(subject: str, body: str) -> str:
    department_match = re.search(r"面试部门[:：]\s*([^\n]+)", body)
    position_match = re.search(r"面试职位[:：]\s*([^\n]+)", body)
    if department_match and position_match:
        return clean_role(f"{department_match.group(1)}{position_match.group(1)}")

    patterns = (
        re.compile(r"面试职位[:：]\s*([^\n]+)"),
        re.compile(r"现邀请您就\s*([^\n，。,]+?)职位进行一次面试"),
        re.compile(r"您应聘的([^\n，。,]+?)下的面试信息发生变化"),
    )
    for pattern in patterns:
        match = pattern.search(body)
        if match:
            return clean_role(match.group(1))

    if "QQ" in body and "客户端开发" in body:
        return "QQ客户端开发"
    if "Java" in subject or "Java" in body:
        return "Java开发实习"
    return ""


def parse_datetime(value: str) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unsupported datetime: {value}")


def format_due(value: dt.datetime, with_seconds: bool = False) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def display_time(value: dt.datetime) -> str:
    return f"{value.month}月{value.day}日 {value.strftime('%H:%M')}"


def parse_timing(mail: CandidateMail, event_type: str, body: str) -> dict[str, str]:
    body = normalize_whitespace(body)

    patterns = (
        re.compile(
            r"面试时间(?:\[北京时间\])?[:：]\s*(\d{4}-\d{2}-\d{2}).*?(\d{1,2}:\d{2})\s*[-~～至]\s*(\d{1,2}:\d{2})",
            re.S,
        ),
        re.compile(
            r"面试日期[:：]\s*(\d{4}-\d{2}-\d{2}).*?面试时间[:：]\s*(\d{1,2}:\d{2})(?:\s*[-~～至]\s*(\d{1,2}:\d{2}))?",
            re.S,
        ),
        re.compile(
            r"(笔试|测评)时间[:：]\s*(\d{4}-\d{2}-\d{2}).*?(\d{1,2}:\d{2})\s*[-~～至]\s*(\d{1,2}:\d{2})",
            re.S,
        ),
        re.compile(
            r"考试开始时间.*?(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2}:\d{2}).*?考试结束时间.*?(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2}:\d{2})",
            re.S,
        ),
    )

    for pattern in patterns:
        match = pattern.search(body)
        if not match:
            continue

        if len(match.groups()) == 4 and "考试开始时间" in pattern.pattern:
            start = parse_datetime(f"{match.group(1)} {match.group(2)}")
            end = parse_datetime(f"{match.group(3)} {match.group(4)}")
            return {
                "type": "scheduled_window",
                "start": format_due(start),
                "end": format_due(end),
            }

        if len(match.groups()) == 4 and match.group(1) in ("笔试", "测评"):
            date_part, start_text, end_text = match.group(2), match.group(3), match.group(4)
        else:
            date_part, start_text = match.group(1), match.group(2)
            end_text = match.group(3) if len(match.groups()) >= 3 else None

        start = parse_datetime(f"{date_part} {start_text}")
        timing: dict[str, str] = {"start": format_due(start)}
        if end_text:
            end = parse_datetime(f"{date_part} {end_text}")
            timing["end"] = format_due(end)
            timing["type"] = "scheduled_window"
        else:
            timing["type"] = "scheduled_start"
        return timing

    explicit_deadline = re.search(
        r"(?:截止时间|截止|完成期限)[:：]\s*(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?)",
        body,
    )
    if explicit_deadline:
        deadline = parse_datetime(explicit_deadline.group(1).replace("T", " "))
        return {"type": "deadline", "deadline": format_due(deadline, with_seconds=True)}

    subject_deadline = re.search(r"请在(\d+)小时内完成", mail.subject)
    if subject_deadline:
        hours = int(subject_deadline.group(1))
        deadline = mail.received_at + dt.timedelta(hours=hours)
        return {"type": "deadline", "deadline": format_due(deadline, with_seconds=True)}

    generic_range = re.search(
        r"(\d{4}-\d{2}-\d{2}).*?(\d{1,2}:\d{2})\s*[-~～至]\s*(\d{1,2}:\d{2})",
        body,
        re.S,
    )
    if generic_range:
        start = parse_datetime(f"{generic_range.group(1)} {generic_range.group(2)}")
        end = parse_datetime(f"{generic_range.group(1)} {generic_range.group(3)}")
        return {
            "type": "scheduled_window",
            "start": format_due(start),
            "end": format_due(end),
        }

    return {"type": "unknown", "observedAt": format_due(mail.received_at)}


def choose_primary_link(event_type: str, urls: list[str]) -> str:
    if not urls:
        return ""

    priority_rules = {
        "interview": ("meeting.tencent.com", "teams", "zoom", "feishu"),
        "ai_interview": ("nowcoder.com/ai-interview", "exam.nowcoder.com", "meeting.tencent.com"),
        "written_exam": ("nowcoder.com", "mokahr.com", "assessment", "exam"),
        "assessment": ("assessment", "nowcoder.com", "mokahr.com"),
        "authorization": ("mokahr.com", "join.qq.com", "nowcoder.com"),
    }
    priorities = priority_rules.get(event_type, ())
    for needle in priorities:
        for url in urls:
            if needle in url:
                return url
    return urls[0]


def extract_links(body: str) -> list[str]:
    urls = URL_RE.findall(body)
    filtered: list[str] = []
    for url in urls:
        cleaned = url.rstrip(".,)")
        if cleaned not in filtered:
            filtered.append(cleaned)
    return filtered


def describe_event_label(event_type: str, subject: str) -> str:
    if event_type == "authorization":
        return "招聘授权"
    if event_type == "ai_interview":
        return "AI面试"
    if event_type == "assessment":
        return "在线测评"
    if "在线专业笔试" in subject:
        return "在线专业笔试"
    if "在线技术笔试" in subject:
        return "在线技术笔试"
    if event_type == "written_exam":
        return "在线笔试"
    return "面试"


def build_title(company: str, event_type: str, role: str, timing: dict[str, str], subject: str) -> str:
    label = describe_event_label(event_type, subject)
    title_base = company
    if role and event_type in ("interview", "ai_interview"):
        title_base += role

    if timing.get("type") == "deadline":
        deadline = parse_datetime(timing["deadline"])
        if event_type == "ai_interview":
            return f"{title_base}{label}（{display_time(deadline)}前完成）"
        return f"{title_base}{label}（{display_time(deadline)}截止）"

    if timing.get("start"):
        start = parse_datetime(timing["start"])
        return f"{title_base}{label}（{display_time(start)}）"

    return f"{title_base}{label}"


def build_note(event_type: str, timing: dict[str, str], role: str, primary_link: str, subject: str) -> str:
    lines: list[str] = []
    if timing.get("type") == "deadline":
        label = "完成期限" if event_type == "ai_interview" else "截止时间"
        lines.append(f"{label}：{timing['deadline']}")
    elif timing.get("start") and timing.get("end"):
        label = "面试时间" if event_type in ("interview", "ai_interview") else "笔试时间"
        lines.append(f"{label}：{timing['start']} - {timing['end'][-5:]}")
    elif timing.get("start"):
        label = "面试时间" if event_type in ("interview", "ai_interview") else "开始时间"
        lines.append(f"{label}：{timing['start']}")

    if role and event_type in ("interview", "ai_interview"):
        lines.append(f"岗位：{role}")

    if event_type == "ai_interview":
        hours_match = re.search(r"请在(\d+)小时内完成", subject)
        if hours_match:
            lines.append(f"说明：收到邮件后 {hours_match.group(1)} 小时内完成")

    if primary_link:
        lines.append(f"入口：{primary_link}")

    return "\n".join(lines)


def build_state_key(company: str, event_type: str, role: str, primary_link: str, subject: str) -> str:
    role_key = clean_role(role).lower()
    link_key = re.sub(r"[?#].*$", "", primary_link)
    if not link_key:
        link_key = subject
    return f"{company}|{event_type}|{role_key}|{link_key}"


def candidate_to_event(mail: CandidateMail) -> Event | None:
    body = normalize_whitespace(mail.body)
    company = detect_company(mail.subject, mail.sender, body)
    event_type = detect_event_type(mail.subject, body)
    role = extract_role(mail.subject, body)
    timing = parse_timing(mail, event_type, body)
    if looks_like_receipt(mail.subject):
        return None
    if company == "待确认公司":
        return None
    if timing.get("type") == "unknown":
        return None

    links = extract_links(body)
    primary_link = choose_primary_link(event_type, links)
    if event_type in ("interview", "written_exam", "assessment", "authorization") and not primary_link:
        return None

    title = build_title(company, event_type, role, timing, mail.subject)
    note = build_note(event_type, timing, role, primary_link, mail.subject)

    if timing.get("type") == "deadline":
        main_due = timing["deadline"]
    else:
        main_due = timing.get("start", mail.received_at.strftime("%Y-%m-%d %H:%M"))

    state_key = build_state_key(company, event_type, role, primary_link, mail.subject)
    link_entries = [{"label": "入口", "url": primary_link}] if primary_link else []
    return Event(
        state_key=state_key,
        source_ids=[mail.thread_id],
        source_subjects=[mail.subject],
        company=company,
        event_type=event_type,
        title=title,
        note=note,
        main_due=main_due,
        timing=timing,
        links=link_entries,
        received_at=mail.received_at,
        role=role,
        source_sender=mail.sender,
    )


def merge_event(existing: Event, incoming: Event) -> Event:
    winner = incoming if incoming.received_at >= existing.received_at else existing
    loser = existing if winner is incoming else incoming
    return Event(
        state_key=winner.state_key,
        source_ids=sorted(set(existing.source_ids + incoming.source_ids)),
        source_subjects=existing.source_subjects + [s for s in incoming.source_subjects if s not in existing.source_subjects],
        company=winner.company,
        event_type=winner.event_type,
        title=winner.title,
        note=winner.note,
        main_due=winner.main_due,
        timing=winner.timing,
        links=winner.links or loser.links,
        received_at=winner.received_at,
        priority=winner.priority,
        role=winner.role or loser.role,
        source_sender=winner.source_sender or loser.source_sender,
    )


def build_events(candidates: list[CandidateMail], mail_account: str) -> list[Event]:
    filtered = [candidate for candidate in candidates if is_candidate(candidate)]
    for candidate in filtered:
        candidate.body = fetch_mail_body(mail_account, candidate.subject)

    merged: dict[str, Event] = {}
    for candidate in filtered:
        event = candidate_to_event(candidate)
        if not event:
            continue
        if event.state_key in merged:
            merged[event.state_key] = merge_event(merged[event.state_key], event)
        else:
            merged[event.state_key] = event

    return sorted(
        merged.values(),
        key=lambda event: (
            parse_datetime(event.main_due if len(event.main_due) > 16 else f"{event.main_due}:00"),
            PRIORITY_EVENT_TYPES.get(event.event_type, 99),
            event.title,
        ),
    )


def write_state(events: list[Event], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schemaVersion": 1,
        "list": DEFAULT_LIST,
        "account": "iCloud",
        "followUpPolicy": "none",
        "source": "gmail_search_plus_mail_fallback",
        "notePolicy": {
            "keep": [
                "中文标题",
                "事件类型对应的真实时间信息",
                "唯一有效入口链接",
                "必要时的一句说明",
            ],
            "drop": [
                "投递成功回执",
                "Gmail ID",
                "发件人元数据",
                "长摘要",
                "与当前事件无关的说明",
            ],
        },
        "processed": {event.source_ids[-1]: event.to_state_entry() for event in events},
    }
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(events: list[Event]) -> None:
    rows = [
        {
            "title": event.title,
            "due": event.main_due,
            "eventType": event.event_type,
            "company": event.company,
            "sourceThreadIds": event.source_ids,
        }
        for event in events
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def sync_reminders(state_file: Path) -> None:
    subprocess.run(
        [sys.executable, str(REMINDERS_SCRIPT), "sync-plan", "--file", str(state_file), "--clear"],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build recruiting reminder state from Gmail + Mail fallback")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="Gmail account for gog, e.g. your@gmail.com")
    parser.add_argument("--mail-account", default="", help='Apple Mail account name, e.g. "谷歌"')
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX)
    parser.add_argument("--output", default=str(STATE_PATH))
    parser.add_argument("--sync-reminders", action="store_true")
    args = parser.parse_args()

    mail_account = discover_mail_account(args.mail_account)
    candidates = load_candidates(args.account, args.days, args.max_results)
    events = build_events(candidates, mail_account)
    output = Path(args.output).expanduser()
    write_state(events, output)
    print_summary(events)
    if args.sync_reminders:
        sync_reminders(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
