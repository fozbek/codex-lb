from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.core.scheduling import leader_election as leader_election_module
from app.db.models import AuditLog, SchedulerLeader
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def db_session(db_setup):
    async with SessionLocal() as session:
        yield session
        if session.in_transaction():
            await session.rollback()


def _enable_leader_election(monkeypatch: pytest.MonkeyPatch, *, ttl_seconds: float = 60) -> None:
    settings = SimpleNamespace(
        leader_election_enabled=True,
        leader_election_ttl_seconds=ttl_seconds,
    )
    monkeypatch.setattr(leader_election_module, "get_settings", lambda: settings)


async def _steal_lease(new_leader_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(SchedulerLeader)
            .where(SchedulerLeader.id == 1)
            .values(
                leader_id=new_leader_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=120),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_cross_instance_rate_limiting(db_session):
    from app.core.exceptions import DashboardRateLimitError
    from app.core.rate_limiter.db_rate_limiter import DatabaseRateLimiter

    instance1 = DatabaseRateLimiter(max_attempts=8, window_seconds=300, type="totp")
    instance2 = DatabaseRateLimiter(max_attempts=8, window_seconds=300, type="totp")

    key = "test-multi-replica-ip"

    for _ in range(4):
        await instance1.check_and_record(key, db_session)

    async with SessionLocal() as instance2_session:
        for _ in range(4):
            await instance2.check_and_record(key, instance2_session)

        with pytest.raises(DashboardRateLimitError):
            await instance2.check_and_record(key, instance2_session)


@pytest.mark.asyncio
async def test_check_and_increment_records_first_password_attempt(db_session):
    from app.core.rate_limiter.db_rate_limiter import DatabaseRateLimiter

    limiter = DatabaseRateLimiter(max_attempts=8, window_seconds=300, type="password")
    key = "test-password-login"

    await limiter.check_and_increment(key, db_session)
    await limiter.clear_for_key(key, db_session)
    await limiter.check_and_increment(key, db_session)


@pytest.mark.asyncio
async def test_settings_cache_consistency(db_session):
    from app.core.config.settings_cache import get_settings_cache

    cache = get_settings_cache()
    await cache.invalidate()

    settings1 = await cache.get()
    settings2 = await cache.get()

    assert settings1 is settings2


@pytest.mark.asyncio
async def test_leader_election_elects_single_leader(db_setup, monkeypatch: pytest.MonkeyPatch):
    """Two replicas over one database: exactly one wins the lease.

    Regression: the old implementation short-circuited to True for every
    process on SQLite, so both instances became leader.
    """
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch)

    election1 = LeaderElection(leader_id="instance-1")
    election2 = LeaderElection(leader_id="instance-2")

    result1, result2 = await asyncio.gather(election1.try_acquire(), election2.try_acquire())

    assert (result1, result2).count(True) == 1
    assert (result1, result2).count(False) == 1


@pytest.mark.asyncio
async def test_leader_election_holder_reacquires_and_follower_stays_out(db_setup, monkeypatch: pytest.MonkeyPatch):
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch)

    election1 = LeaderElection(leader_id="instance-1")
    election2 = LeaderElection(leader_id="instance-2")

    assert await election1.try_acquire() is True
    assert await election2.try_acquire() is False
    assert await election1.try_acquire() is True
    assert await election2.try_acquire() is False


@pytest.mark.asyncio
async def test_leader_election_follower_takes_over_after_release(db_setup, monkeypatch: pytest.MonkeyPatch):
    """Regression: the old implementation never released the lease, so a
    follower had to wait out the full TTL after a graceful shutdown."""
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch)

    election1 = LeaderElection(leader_id="instance-1")
    election2 = LeaderElection(leader_id="instance-2")

    assert await election1.try_acquire() is True
    assert await election2.try_acquire() is False

    await election1.release()

    assert election1.is_leader is False
    assert await election2.try_acquire() is True


@pytest.mark.asyncio
async def test_leader_election_follower_takes_over_after_expiry(db_setup, monkeypatch: pytest.MonkeyPatch):
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch, ttl_seconds=0.2)

    election1 = LeaderElection(leader_id="instance-1")
    election2 = LeaderElection(leader_id="instance-2")

    assert await election1.try_acquire() is True
    assert await election2.try_acquire() is False

    await asyncio.sleep(0.3)

    assert await election2.try_acquire() is True


@pytest.mark.asyncio
async def test_leader_election_renew_demotes_when_lease_stolen(db_setup, monkeypatch: pytest.MonkeyPatch):
    """Regression: the old renew() ignored the UPDATE rowcount and reported
    success even after the lease had been taken over."""
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch)

    election1 = LeaderElection(leader_id="instance-1")
    assert await election1.try_acquire() is True
    assert await election1.renew() is True

    await _steal_lease("thief")

    assert await election1.renew() is False
    assert election1.is_leader is False


@pytest.mark.asyncio
async def test_run_if_leader_heartbeat_keeps_follower_out_during_long_body(db_setup, monkeypatch: pytest.MonkeyPatch):
    """Regression: the old implementation never renewed during a task, so a
    body outliving the TTL let a second replica become a concurrent leader."""
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch, ttl_seconds=2)

    election1 = LeaderElection(leader_id="instance-1")
    election2 = LeaderElection(leader_id="instance-2")
    body_done = asyncio.Event()

    async def _long_body() -> str:
        # Outlives the 2s TTL; the heartbeat (interval 1s) must keep the
        # lease alive the whole time.
        await asyncio.sleep(2.6)
        body_done.set()
        return "finished"

    async def _poll_follower() -> list[bool]:
        attempts: list[bool] = []
        while not body_done.is_set():
            attempts.append(await election2.try_acquire())
            await asyncio.sleep(0.3)
        return attempts

    result, attempts = await asyncio.gather(
        election1.run_if_leader(_long_body),
        _poll_follower(),
    )

    assert result == "finished"
    assert attempts
    assert not any(attempts)


@pytest.mark.asyncio
async def test_run_if_leader_cancels_body_when_lease_lost(db_setup, monkeypatch: pytest.MonkeyPatch):
    from app.core.scheduling.leader_election import LeaderElection

    _enable_leader_election(monkeypatch, ttl_seconds=2)

    election1 = LeaderElection(leader_id="instance-1")
    body_started = asyncio.Event()
    body_cancelled = asyncio.Event()

    async def _long_body() -> str:
        body_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            body_cancelled.set()
            raise
        return "finished"

    task = asyncio.create_task(election1.run_if_leader(_long_body))
    await asyncio.wait_for(body_started.wait(), timeout=2)

    await _steal_lease("thief")

    # The heartbeat renews every 1s, observes rowcount 0 and cancels the body.
    result = await asyncio.wait_for(task, timeout=5)

    assert result is None
    assert body_cancelled.is_set()
    assert election1.is_leader is False


@pytest.mark.asyncio
async def test_automations_scheduler_tick_body_runs_once_across_two_replicas(db_setup, monkeypatch: pytest.MonkeyPatch):
    """Two automations scheduler replicas tick concurrently over one database;
    the tick body must run exactly once.

    Regression: with the old SQLite everyone-is-leader bypass both replicas
    executed the tick body.
    """
    import app.modules.automations.scheduler as automations_scheduler_module
    from app.core.scheduling.leader_election import LeaderElection
    from app.modules.automations.scheduler import AutomationsScheduler

    _enable_leader_election(monkeypatch)

    elections = [LeaderElection(leader_id="instance-1"), LeaderElection(leader_id="instance-2")]
    monkeypatch.setattr(automations_scheduler_module, "_get_leader_election", lambda: elections.pop(0))

    body_runs = 0

    async def _counting_body(self: AutomationsScheduler) -> None:
        nonlocal body_runs
        body_runs += 1

    monkeypatch.setattr(AutomationsScheduler, "_run_due_as_leader", _counting_body)

    scheduler1 = AutomationsScheduler(interval_seconds=60, enabled=True)
    scheduler2 = AutomationsScheduler(interval_seconds=60, enabled=True)

    await asyncio.gather(scheduler1._run_due_once(), scheduler2._run_due_once())

    assert body_runs == 1


@pytest.mark.asyncio
async def test_audit_log_records_from_different_modules(db_session):
    from app.core.audit.service import _write_audit_log

    await _write_audit_log(
        "account_created",
        actor_ip="1.2.3.4",
        details={"name": "test"},
        request_id="req-1",
    )
    await _write_audit_log(
        "api_key_created",
        actor_ip="1.2.3.5",
        details={"name": "key1"},
        request_id="req-2",
    )

    logs = (await db_session.execute(select(AuditLog))).scalars().all()
    assert len(logs) >= 2
    actions = {log.action for log in logs}
    assert "account_created" in actions
    assert "api_key_created" in actions
