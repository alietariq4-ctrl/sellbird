"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval.
"""

import asyncio
import email
import email.policy
import hashlib
import imaplib
import inspect
import logging
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import aiosmtplib

from sellbird.integrations.mail_provider import (
    InboundMessage,
    MailIdentity,
    MailProvider,
    SendResult,
    identity_from_config,
    looks_like_bounce,
)
from sellbird.integrations.signature import SignatureError, load_signature, set_body

_AUTO_REPLY_HEADERS = (
    "Auto-Submitted", "X-Autoreply", "X-Autorespond", "X-Autoresponse",
    "Precedence", "References", "X-Failed-Recipients", "Final-Recipient", "Original-Recipient",
)
logger = logging.getLogger("sellbird.smtp")


# Refused or never connected: the server definitely has no copy. Anything else
# (a drop or timeout mid-conversation, an unknown error) might have come after
# DATA was accepted, so the outcome is uncertain.
_DEFINITE_SMTP_ERRORS = (
    aiosmtplib.SMTPConnectError, aiosmtplib.SMTPConnectTimeoutError,
    aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPHeloError,
    aiosmtplib.SMTPNotSupported, aiosmtplib.SMTPRecipientsRefused,
    aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPDataError,
)


def _open_imap(host: str, port: int, timeout: float):
    """Open IMAP4_SSL with timeout when supported by the target class."""
    try:
        sig = inspect.signature(imaplib.IMAP4_SSL)
        accepts_timeout = (
            "timeout" in sig.parameters
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        )
    except (TypeError, ValueError):
        accepts_timeout = True
    if accepts_timeout:
        return imaplib.IMAP4_SSL(host, port, timeout=timeout)
    return imaplib.IMAP4_SSL(host, port)


@dataclass
class SmtpSettings:
    """Another inbox's server settings, for a caller that keeps them itself.
    Store the password somewhere safe; only public() is fit for a database."""

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 587
    imap_host: str = ""
    imap_port: int = 993

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password")
        return data


_SMTP_ONLY_RELAY_SUFFIXES = (
    "resend.com",
    "sendgrid.net",
    "postmarkapp.com",
    "mailgun.org",
    "amazonaws.com",
    "sparkpostmail.com",
    "brevo.com",
    "mailjet.com",
)


class SmtpImapProvider(MailProvider):
    name = "smtp"
    carries_headers = True

    def __init__(self, config, env, *, identity: MailIdentity | None = None,
                 smtp: SmtpSettings | None = None):
        self.config = config
        # Settable: a mailbox transport points it at the inbox it sends from.
        self.identity = identity or identity_from_config(config)
        if smtp is not None:  # another inbox: its own settings, not the .env's
            self.smtp_host, self.smtp_port = smtp.host, int(smtp.port or 587)
            self.smtp_user, self.smtp_pass = smtp.username, smtp.password
            self.smtp_from = ""
            self._imap_explicit = bool((smtp.imap_host or "").strip())
            self.imap_host = smtp.imap_host or smtp.host
            self.imap_port = int(smtp.imap_port or 993)
            self.imap_user, self.imap_pass = smtp.username, smtp.password
            return
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.smtp_from = (getattr(env, "smtp_from", "") or os.getenv("SMTP_FROM", "") or "").strip()
        raw_imap_host = (getattr(env, "imap_host", "") or "").strip()
        self._imap_explicit = bool(raw_imap_host)
        self.imap_host = raw_imap_host or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass

    @property
    def username(self) -> str:
        return self.imap_user or self.smtp_user or (self.identity.address if self.identity else "")

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers=(),
    ) -> SendResult:
        try:
            sig = load_signature(self.config)
        except SignatureError as e:
            return SendResult(ok=False, error=f"signature: {e}"[:300])
        address = self.smtp_from or self.identity.address or self.smtp_user
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = formataddr((self.identity.display_name, address))
        msg["Subject"] = subject
        for name, value in headers:
            msg[name] = value
        message_id = make_msgid(domain=address.split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        set_body(msg, body, sig)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300],
                              uncertain=not isinstance(e, _DEFINITE_SMTP_ERRORS))
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unseen, limit)
        except OSError:
            logger.error("IMAP connection error while fetching replies")
            raise
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            raise RuntimeError(f"IMAP fetch failed: {e}") from e

    def _own_addresses(self) -> set[str]:
        addrs = {
            (self.imap_user or "").strip().lower(),
            (self.smtp_user or "").strip().lower(),
            (self.identity.address if self.identity else "").strip().lower(),
        }
        return {a for a in addrs if a and "@" in a}

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        own = self._own_addresses()
        conn = _open_imap(self.imap_host, self.imap_port, 30)
        try:
            conn.login(self.imap_user, self.imap_pass)
            sel_status, _ = conn.select("INBOX")
            if sel_status != "OK":
                raise RuntimeError(f"IMAP select INBOX failed ({sel_status})")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                raise RuntimeError(f"IMAP search UNSEEN failed ({status})")
            all_ids = (data[0] or b"").split()
            if limit and len(all_ids) > limit:
                raise RuntimeError(
                    f"IMAP inbox has {len(all_ids)} UNSEEN messages (> limit {limit}); "
                    "refusing a partial read"
                )
            for msg_id in all_ids:
                # Use BODY.PEEK[] so unread messages are NOT destructively marked \Seen
                # before Handler finishes classifying and recording them.
                status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not parts:
                    continue
                raw_bytes = None
                for part in parts:
                    if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                        raw_bytes = bytes(part[1])
                        break
                if not raw_bytes:
                    continue
                msg_uid = msg_id.decode(errors="replace") if isinstance(msg_id, (bytes, bytearray)) else str(msg_id)
                parsed = self._parse_rfc822(raw_bytes, uid=msg_uid)
                if parsed and parsed.from_email.lower() not in own:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    @staticmethod
    def _parse_rfc822(raw: bytes, uid: str = "") -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
            if msg.is_multipart():
                dsn_chunks = []
                for subpart in msg.walk():
                    if subpart.get_content_type() in ("message/delivery-status", "text/rfc822-headers"):
                        try:
                            dsn_chunks.append(subpart.as_string())
                        except Exception:
                            pass
                if dsn_chunks:
                    body = (body + "\n" + "\n".join(dsn_chunks)).strip()
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        date_str = str(msg.get("Date", "")).strip()
        if message_id:
            provider_id = message_id
        else:
            parts = [from_email, subject]
            if date_str:
                parts.append(date_str)
            if uid:
                parts.append(str(uid))
            if not date_str and not uid:
                parts.append(hashlib.sha256(raw).hexdigest()[:12])
            provider_id = ":".join(parts)
        return InboundMessage(
            provider_id=provider_id,
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
            # Hosted H7: what tells an automatic reply from a person's.
            headers={name.lower(): str(msg.get(name, "")) for name in _AUTO_REPLY_HEADERS
                     if msg.get(name)},
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # Outbound-only SMTP relays (Resend, SendGrid, Postmark, etc.) don't offer IMAP.
        host_low = (self.smtp_host or "").strip().lower()
        if not getattr(self, "_imap_explicit", True) and any(
            host_low.endswith(suf) for suf in _SMTP_ONLY_RELAY_SUFFIXES
        ):
            return True, f"SMTP connected (outbound relay as {self.smtp_user}; no IMAP inbox on {self.smtp_host})"
        # IMAP handshake
        loop = asyncio.get_running_loop()

        def _imap_check():
            conn = _open_imap(self.imap_host, self.imap_port, 15)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
