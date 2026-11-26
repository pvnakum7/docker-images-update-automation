"""SMTP delivery of the consolidated report (TLS/STARTTLS, multiple recipients, attachments)."""
from __future__ import annotations

import mimetypes
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from .logger import get_logger, register_secret
from .report_generator import OverallSummary

log = get_logger("email")

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_NAMED_RE = re.compile(r"[^<,;]*<([^<>]+)>")


def normalize_email(addr: str) -> str:
    return addr.strip().strip("<>\"'").strip().lower()


def is_valid_email(addr: str) -> bool:
    return len(addr) <= 254 and bool(_EMAIL_RE.match(addr))


def mask(token: str) -> str:
    """Log-safe rendering of a bad token: never print more than 2 leading characters."""
    t = token.strip()
    return (t[:2] + "***") if len(t) > 2 else "***"


def parse_addresses(raw) -> tuple[list[str], list[str]]:
    """Split a cell on commas / semicolons / whitespace / newlines (also 'Name <a@b.c>').

    Returns (valid normalised addresses in order - duplicates kept, invalid raw tokens)."""
    text = "" if raw is None else str(raw)
    text = _NAMED_RE.sub(lambda m: " " + m.group(1) + " ", text)
    valid, invalid = [], []
    for tok in re.split(r"[,;\s]+", text):
        tok = normalize_email(tok)
        if not tok:
            continue
        (valid if is_valid_email(tok) else invalid).append(tok)
    return valid, invalid


@dataclass
class RecipientPlan:
    to: list[str]
    cc: list[str]
    repositories: int = 0
    duplicates_removed: int = 0
    invalid: list[tuple[str, str]] = field(default_factory=list)   # (repository, masked token)


def collect_recipients(rows) -> RecipientPlan:
    """rows: iterable of (repo_name, email_cell, cc_cell).

    Global, case/whitespace-insensitive de-duplication. TO wins over CC. An address appears once."""
    to: dict[str, None] = {}
    cc: dict[str, None] = {}
    seen_total = 0
    invalid: list[tuple[str, str]] = []
    n = 0
    for repo, email_cell, cc_cell in rows:
        n += 1
        v, bad = parse_addresses(email_cell)
        v2, bad2 = parse_addresses(cc_cell)
        invalid += [(repo, mask(t)) for t in bad + bad2]
        seen_total += len(v) + len(v2)
        for a in v:
            to.setdefault(a)
        for a in v2:
            cc.setdefault(a)
    final_cc = [a for a in cc if a not in to]
    return RecipientPlan(list(to), final_cc, n, seen_total - len(to) - len(final_cc), invalid)


def format_recipient_summary(plan: RecipientPlan) -> str:
    return (
        "Consolidated Email Notification Summary:\n\n"
        f"Total repositories processed: {plan.repositories}\n\n"
        "Application Owner Recipients (TO):\n"
        f"  Total unique recipients: {len(plan.to)}\n\n"
        "Developer Recipients (CC):\n"
        f"  Total unique recipients: {len(plan.cc)}\n\n"
        f"Duplicate email addresses removed: {plan.duplicates_removed}\n\n"
        f"Invalid email addresses skipped: {len(plan.invalid)}\n")


@dataclass
class EmailConfig:
    server: str
    port: int
    username: str
    password: str
    sender: str
    to: list[str]
    cc: list[str]

    @property
    def configured(self) -> bool:
        return bool(self.server and self.sender and self.to)   # `to` is filled from the Excel file at run time


@dataclass
class EmailResult:
    sent: bool
    message: str


def split_addresses(raw: Optional[str]) -> list[str]:
    """Valid, normalised, de-duplicated addresses from a free-text list."""
    return list(dict.fromkeys(parse_addresses(raw)[0]))


def config_from_env(env: Optional[dict] = None) -> EmailConfig:
    env = os.environ if env is None else env
    pw = env.get("SMTP_PASSWORD", "")
    register_secret(pw)
    try:
        port = int(env.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    return EmailConfig(env.get("SMTP_SERVER", ""), port, env.get("SMTP_USERNAME", ""), pw,
                       env.get("EMAIL_FROM", ""), split_addresses(env.get("EMAIL_TO")), split_addresses(env.get("EMAIL_CC")))


def build_body(s: OverallSummary) -> str:
    return (
        "Hello Team,\n\n"
        "The automated Docker image version update process has completed.\n\n"
        "Execution Summary:\n\n"
        f"Date: {s.date}\n"
        f"Mode: {s.mode}\n"
        f"Status: {s.status}\n"
        f"Scheduled Time (UTC): {s.scheduled_time or 'manual / n.a.'}\n"
        f"Started: {s.started}\n"
        f"Finished: {s.finished}\n"
        f"Duration: {s.duration}\n\n"
        f"Total Repositories: {s.total_repos} (processed: {s.ok_repos}, failed: {s.failed_repos})\n"
        f"Total Dockerfiles: {s.total_dockerfiles}\n"
        f"Updated Dockerfiles: {s.updated_dockerfiles}\n"
        f"Skipped Dockerfiles: {s.skipped_dockerfiles}\n"
        f"Failed Dockerfiles: {s.failed_dockerfiles}\n"
        f"Up-to-date Dockerfiles: {s.up_to_date_dockerfiles}\n"
        f"Dockerfiles with pending updates (not applied): {s.pending_dockerfiles}\n\n"
        f"Total FROM Instructions: {s.total_from}\n"
        f"Updated Images: {s.updated_images}\n"
        f"Skipped Images: {s.skipped_images}\n"
        f"Failed Images: {s.failed_images}\n"
        f"Up-to-date Images: {s.up_to_date_images}\n"
        f"Major Upgrade Recommendations: {s.major_recommendations}\n\n"
        "Please find the consolidated CSV report and individual repository reports attached.\n\n"
        "Regards,\nDevOps Automation\n")


def build_message(cfg: EmailConfig, subject: str, summary: OverallSummary, attachments: list[Path]) -> EmailMessage:
    for field_name, val in (("subject", subject), ("sender", cfg.sender)):
        if "\n" in val or "\r" in val:
            raise ValueError(f"illegal newline in email {field_name}")
    for a in cfg.to + cfg.cc:
        if not is_valid_email(a):
            raise ValueError(f"invalid email address: {a!r}")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.to)
    if cfg.cc:
        msg["Cc"] = ", ".join(cfg.cc)
    msg.set_content(build_body(summary))
    for p in attachments:
        p = Path(p)
        if not p.exists():
            log.warning("attachment missing, skipped: %s", p)
            continue
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send_report(cfg: EmailConfig, subject: str, summary: OverallSummary, attachments: list[Path],
                tls_mode: str = "auto", timeout: int = 60, smtp_factory=None) -> EmailResult:
    """Never raises: email problems are reported in the result so the run can still finish."""
    if not cfg.to:
        return EmailResult(False, "no valid TO recipient (Email column of the Excel file) - email skipped")
    if not (cfg.server and cfg.sender):
        return EmailResult(False, "SMTP_SERVER / EMAIL_FROM are not configured - email skipped")
    try:
        msg = build_message(cfg, subject, summary, attachments)
    except (ValueError, OSError) as exc:
        return EmailResult(False, f"could not build email: {exc}")
    recipients = list(dict.fromkeys(cfg.to + [a for a in cfg.cc if a not in cfg.to]))
    mode = tls_mode
    if mode == "auto":
        mode = "ssl" if cfg.port == 465 else "starttls"
    ctx = ssl.create_default_context()
    try:
        if smtp_factory is not None:
            srv = smtp_factory(cfg.server, cfg.port, timeout=timeout)
        elif mode == "ssl":
            srv = smtplib.SMTP_SSL(cfg.server, cfg.port, timeout=timeout, context=ctx)
        else:
            srv = smtplib.SMTP(cfg.server, cfg.port, timeout=timeout)
        with srv:
            if mode == "starttls":
                srv.starttls(context=ctx)
            if cfg.username:
                srv.login(cfg.username, cfg.password)
            srv.send_message(msg, from_addr=cfg.sender, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError:
        return EmailResult(False, "SMTP authentication failed (check SMTP_USERNAME/SMTP_PASSWORD)")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return EmailResult(False, f"SMTP error: {type(exc).__name__}: {str(exc)[:150]}")
    return EmailResult(True, f"email sent to {len(recipients)} recipient(s)")
