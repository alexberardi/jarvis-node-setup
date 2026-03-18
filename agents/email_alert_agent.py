"""EmailAlertAgent — proactive email notifications for VIP senders, urgent messages, and daily digest.

Runs every 5 minutes. Fetches recent unread emails via Gmail API and generates
alerts based on three behaviors:
- VIP senders (priority 3) — configurable email list
- Urgent keywords (priority 2) — subject/snippet keyword matching
- Daily digest (priority 1) — morning summary of unread count + top senders

Does not run on startup to let TokenRefreshAgent warm up Gmail tokens first.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.alert import Alert
from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from jarvis_services.email_message import extract_email
from jarvis_services.email_service_factory import create_email_service, get_email_provider
from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
ALERT_TTL_HOURS = 8
MAX_ALERTS_PER_RUN = 5
MAX_DEDUP_CACHE = 200

DEFAULT_URGENT_KEYWORDS: set[str] = {
    "urgent",
    "asap",
    "emergency",
    "action required",
    "immediate",
    "critical",
    "deadline",
}


class EmailAlertAgent(IJarvisAgent):
    """Background agent that monitors Gmail for important emails."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._alerted_email_ids: set[str] = set()
        self._last_digest_date: str = ""  # ISO date string, e.g. "2026-03-15"
        self._vip_senders: set[str] = set()  # cached from secrets

    @property
    def name(self) -> str:
        return "email_alerts"

    @property
    def description(self) -> str:
        return "Monitors Gmail for VIP emails, urgent messages, and daily digest"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=False,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "EMAIL_ALERT_VIP_SENDERS",
                "Comma-separated VIP email addresses for high-priority alerts",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="VIP Senders",
            ),
            JarvisSecret(
                "EMAIL_ALERT_URGENT_KEYWORDS",
                "Comma-separated keywords that trigger urgent email alerts",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Urgent Keywords",
            ),
            JarvisSecret(
                "EMAIL_ALERT_DIGEST_HOUR",
                "Hour (0-23) for daily email digest (default: 7)",
                "integration",
                "int",
                required=False,
                is_sensitive=False,
                friendly_name="Digest Hour",
            ),
        ]

    def validate_secrets(self) -> List[str]:
        """Agent requires email credentials to function."""
        provider = get_email_provider()
        if provider == "imap":
            missing: list[str] = []
            if not get_secret_value("IMAP_USERNAME", "integration"):
                missing.append("IMAP_USERNAME")
            if not get_secret_value("IMAP_PASSWORD", "integration"):
                missing.append("IMAP_PASSWORD")
            return missing
        # Gmail
        if not get_secret_value("GMAIL_ACCESS_TOKEN", "integration"):
            return ["GMAIL_ACCESS_TOKEN"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Fetch recent unread emails and generate alerts."""
        try:
            # Validate credentials exist before constructing service
            missing = self.validate_secrets()
            if missing:
                self._alerts = []
                return

            try:
                service = create_email_service()
            except ValueError:
                self._alerts = []
                return

            emails = service.search("is:unread in:inbox newer_than:1d", max_results=20)

            # Load config from secrets
            vip_senders = self._load_vip_senders()
            urgent_keywords = self._load_urgent_keywords()
            digest_hour = self._load_digest_hour()

            now = datetime.now(timezone.utc)
            all_alerts: List[Alert] = []

            for email in emails:
                # VIP check first (higher priority)
                vip_alerts = self._check_vip(email, vip_senders)
                all_alerts.extend(vip_alerts)

                # Urgent keyword check (skips already-alerted emails)
                urgent_alerts = self._check_urgent(email, urgent_keywords)
                all_alerts.extend(urgent_alerts)

            # Daily digest
            digest_alerts = self._check_digest(emails, digest_hour, now)
            all_alerts.extend(digest_alerts)

            self._alerts = self._apply_rate_limit(all_alerts)

            # Trim dedup cache
            if len(self._alerted_email_ids) > MAX_DEDUP_CACHE:
                # Keep the most recent entries (arbitrary trim)
                excess = len(self._alerted_email_ids) - MAX_DEDUP_CACHE
                to_remove = list(self._alerted_email_ids)[:excess]
                for item in to_remove:
                    self._alerted_email_ids.discard(item)

            if self._alerts:
                logger.info("Email agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Email alert agent run failed", error=str(e))
            self._alerts = []

    def _load_vip_senders(self) -> set[str]:
        """Load VIP sender list from secrets, falling back to cached value."""
        raw = get_secret_value("EMAIL_ALERT_VIP_SENDERS", "integration")
        if raw:
            self._vip_senders = {
                addr.strip().lower() for addr in raw.split(",") if addr.strip()
            }
        return self._vip_senders

    def _load_urgent_keywords(self) -> set[str]:
        """Load urgent keywords from secrets or use defaults."""
        raw = get_secret_value("EMAIL_ALERT_URGENT_KEYWORDS", "integration")
        if raw:
            return {kw.strip().lower() for kw in raw.split(",") if kw.strip()}
        return DEFAULT_URGENT_KEYWORDS

    def _load_digest_hour(self) -> int:
        """Load digest hour from secrets or default to 7."""
        raw = get_secret_value("EMAIL_ALERT_DIGEST_HOUR", "integration")
        if raw:
            try:
                hour = int(raw)
                if 0 <= hour <= 23:
                    return hour
            except ValueError:
                pass
        return 7

    def _check_vip(self, email: Any, vip_senders: set[str]) -> List[Alert]:
        """Check if email is from a VIP sender. Returns 0 or 1 alerts."""
        if not vip_senders:
            return []

        if email.id in self._alerted_email_ids:
            return []

        sender_email = extract_email(email.sender).lower()
        if sender_email not in vip_senders:
            return []

        now = datetime.now(timezone.utc)
        self._alerted_email_ids.add(email.id)

        return [Alert(
            source_agent=self.name,
            title=f"Email from {email.sender_name}",
            summary=f"{email.sender_name}: {email.subject}",
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=3,
        )]

    def _check_urgent(self, email: Any, keywords: set[str]) -> List[Alert]:
        """Check if email subject/snippet contains urgent keywords. Returns 0 or 1 alerts."""
        if email.id in self._alerted_email_ids:
            return []

        text = f"{email.subject} {email.snippet}".lower()
        matched = any(kw in text for kw in keywords)

        if not matched:
            return []

        now = datetime.now(timezone.utc)
        self._alerted_email_ids.add(email.id)

        return [Alert(
            source_agent=self.name,
            title=f"Urgent: {email.subject}",
            summary=f"From {email.sender_name}: {email.subject}",
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=2,
        )]

    def _check_digest(
        self, emails: list[Any], digest_hour: int, now: datetime
    ) -> List[Alert]:
        """Generate a daily digest alert during the morning window."""
        if not emails:
            return []

        # Only trigger during the configured hour
        if now.hour != digest_hour:
            return []

        today = now.strftime("%Y-%m-%d")
        if self._last_digest_date == today:
            return []

        self._last_digest_date = today

        # Count emails per sender
        sender_counts: Counter[str] = Counter()
        for email in emails:
            sender_counts[email.sender_name] += 1

        # Top 3 senders
        top_senders = sender_counts.most_common(3)
        top_str = ", ".join(f"{name} ({count})" for name, count in top_senders)

        return [Alert(
            source_agent=self.name,
            title="Daily Email Digest",
            summary=f"{len(emails)} unread emails. Top senders: {top_str}",
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=1,
        )]

    def _apply_rate_limit(self, alerts: List[Alert]) -> List[Alert]:
        """Cap alerts per run, keeping highest priority first."""
        if len(alerts) <= MAX_ALERTS_PER_RUN:
            return alerts
        # Sort by priority descending, take top N
        alerts.sort(key=lambda a: a.priority, reverse=True)
        return alerts[:MAX_ALERTS_PER_RUN]

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
