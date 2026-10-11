"""Login throttling with backoff, kept in the database so every replica shares it.

Failures are counted per *key*. A key is a source address, or an account together with a source
address, so an anonymous caller can slow down their own guesses but cannot lock the real
administrator out from the administrator's own address. After ``free`` failures the key is blocked
for ``base * 2**(failures - free)`` seconds (at most ``ceiling``); a failure more than ``window``
after the previous one starts the count again, and a success clears the key.

Example:
    >>> backoff_seconds(5, free=5), backoff_seconds(7, free=5), backoff_seconds(40, free=5)
    (30, 120, 900)
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from switchyard.db.auth_models import LoginThrottleRow
from switchyard.db.tenancy import admin_session
from switchyard.services.context import Clock, Databases, utcnow

BASE_S = 30
CEILING_S = 900
WINDOW = timedelta(hours=1)


def backoff_seconds(failures: int, *, free: int) -> int:
    """How long a key with ``failures`` failures is blocked (0 while within the free tries)."""
    if failures < free:
        return 0
    return int(min(CEILING_S, BASE_S * 2 ** min(failures - free, 10)))


@dataclass(frozen=True)
class Rule:
    """A throttle key and how many failures it tolerates before blocking."""

    key: str
    free: int


class LoginThrottle:
    """Check, count and clear failed sign-ins."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the throttle."""
        self._db = db
        self._clock = clock

    async def blocked_for(self, rules: Sequence[Rule]) -> int:
        """Seconds until the longest-blocked key may try again; 0 if none is blocked."""
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    select(LoginThrottleRow).where(LoginThrottleRow.key.in_([r.key for r in rules]))
                )
            ).scalars()
            waits = [
                int((row.blocked_until - now).total_seconds()) + 1
                for row in rows
                if row.blocked_until and row.blocked_until > now
            ]
        return max(waits, default=0)

    async def record_failure(self, rules: Sequence[Rule]) -> None:
        """Count a failed sign-in against every key and block the ones past their free tries."""
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            for rule in rules:
                row = (
                    await session.execute(
                        select(LoginThrottleRow)
                        .where(LoginThrottleRow.key == rule.key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = LoginThrottleRow(key=rule.key, failures=0, updated_at=now)
                    session.add(row)
                elif now - row.updated_at > WINDOW:
                    row.failures = 0
                row.failures += 1
                row.updated_at = now
                wait = backoff_seconds(row.failures, free=rule.free)
                row.blocked_until = now + timedelta(seconds=wait) if wait else None

    async def clear(self, keys: Sequence[str]) -> None:
        """Forget the failures of ``keys`` (after a successful sign-in)."""
        async with admin_session(self._db.admin) as session:
            await session.execute(delete(LoginThrottleRow).where(LoginThrottleRow.key.in_(keys)))

    async def purge(self, older_than: datetime) -> int:
        """Delete keys idle since ``older_than`` (housekeeping). Returns how many."""
        async with admin_session(self._db.admin) as session:
            result = await session.execute(
                delete(LoginThrottleRow).where(LoginThrottleRow.updated_at < older_than)
            )
            return int(getattr(result, "rowcount", 0) or 0)


#: Failures tolerated per source address (all accounts), and per account from one source.
SOURCE_FREE = 10
ACCOUNT_FREE = 5


def source_rule(source: str) -> Rule:
    """The rule for everything coming from one source address."""
    return Rule(f"src:{source}", SOURCE_FREE)


def account_rule(account: str, source: str) -> Rule:
    """The rule for one account as tried from one source address."""
    return Rule(f"acct:{account}:{source}", ACCOUNT_FREE)
