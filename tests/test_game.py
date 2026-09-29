"""Tests direkt am Spielkern, ohne HTTP."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from pixelkrieg.event import BEFORE, ENDED, NIGHT_PAUSE, RUNNING, event_phase
from pixelkrieg.game import Game, GameError
from pixelkrieg.pricing import price_cents
from pixelkrieg.ratelimit import SlidingWindowLimiter

from .conftest import make_settings


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def game(tmp_path):
    g = Game(make_settings(tmp_path))
    g.open()
    yield g
    g.close()


async def user_with(game, handle, cents):
    uid = await game.get_or_create_user(handle)
    await game.credit_topup("test", f"ref-{handle}-{cents}", uid, cents)
    return uid


def test_price_formula():
    assert price_cents(0) == 2
    assert price_cents(1) == 2  # 2.3 → 2
    assert price_cents(5) == 4  # 4.02 → 4
    assert price_cents(10) == 8  # 8.09 → 8
    assert all(isinstance(price_cents(h), int) for h in range(60))


def test_empty_pixel_keeps_heat_overpaint_raises_it(game):
    async def scenario():
        uid = await user_with(game, "a", 1000)
        r1 = await game.paint(uid, 5, 5, 1)
        assert (r1.price_cents, r1.heat) == (2, 0)
        # Nachbarpixel in derselben Zone, auch leer → Heat bleibt 0
        r2 = await game.paint(uid, 6, 5, 1)
        assert r2.heat == 0
        # Übermalen → Heat steigt, und zwar für die ganze Zone
        r3 = await game.paint(uid, 5, 5, 2)
        assert r3.heat == 1
        assert game.heat[0, 0] == 1
        # Andere Zone bleibt unberührt
        assert game.heat[0, 1] == 0
        assert game.canvas[5, 5] == 2
        return uid

    uid = run(scenario())
    user = game.get_user(uid)
    assert user["balance_cents"] == 1000 - 2 - 2 - 2


def test_price_rises_with_heat(game):
    async def scenario():
        uid = await user_with(game, "a", 2500)
        await game.paint(uid, 0, 0, 1)
        prices = []
        for i in range(12):
            r = await game.paint(uid, 0, 0, 1 + (i % 2) + 1)
            prices.append(r.price_cents)
        return prices

    prices = run(scenario())
    assert prices == [price_cents(h) for h in range(12)]
    assert prices[-1] > prices[0]


def test_cannot_go_negative_even_with_parallel_clicks(game):
    """Der Doppelklick-Fall: viele gleichzeitige Anfragen, Guthaben reicht nur für wenige."""

    async def scenario():
        uid = await user_with(game, "a", 10)  # reicht für genau 5 Pixel à 2 ct
        tasks = [game.paint(uid, x, 0, 1) for x in range(20)]
        return uid, await asyncio.gather(*tasks, return_exceptions=True)

    uid, results = run(scenario())
    ok = [r for r in results if not isinstance(r, Exception)]
    failed = [r for r in results if isinstance(r, GameError)]
    assert len(ok) == 5
    assert len(failed) == 15
    assert all(e.code == "insufficient_funds" for e in failed)
    assert game.get_user(uid)["balance_cents"] == 0


def test_price_changed_is_rejected_not_charged(game):
    async def scenario():
        uid = await user_with(game, "a", 2500)
        await game.paint(uid, 0, 0, 1)
        for i in range(5):
            await game.paint(uid, 0, 0, 2 + i % 2)
        before = game.get_user(uid)["balance_cents"]
        with pytest.raises(GameError) as e:
            await game.paint(uid, 1, 1, 1, max_price_cents=2)
        assert e.value.code == "price_changed"
        assert game.get_user(uid)["balance_cents"] == before

    run(scenario())


def test_same_color_same_owner_rejected(game):
    async def scenario():
        uid = await user_with(game, "a", 100)
        await game.paint(uid, 0, 0, 1)
        with pytest.raises(GameError) as e:
            await game.paint(uid, 0, 0, 1)
        assert e.value.code == "no_change"

    run(scenario())


def test_color_zero_and_out_of_bounds_rejected(game):
    async def scenario():
        uid = await user_with(game, "a", 100)
        for args in [(0, 0, 0), (400, 0, 1), (0, -1, 1), (0, 0, 99)]:
            with pytest.raises(GameError):
                await game.paint(uid, *args)

    run(scenario())


def test_banned_user_cannot_paint(game):
    async def scenario():
        uid = await user_with(game, "a", 100)
        game._writer.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (uid,))
        with pytest.raises(GameError) as e:
            await game.paint(uid, 0, 0, 1)
        assert e.value.code == "banned"
        assert game.get_user(uid)["balance_cents"] == 100  # Guthaben bleibt erhalten

    run(scenario())


def test_cooldown(game):
    async def scenario():
        uid = await user_with(game, "a", 1000)
        t0 = datetime.now(timezone.utc)
        await game.paint(uid, 0, 0, 1, now=t0)
        await game.paint(uid, 0, 0, 2, now=t0)
        await game.paint(uid, 0, 0, 3, now=t0)
        assert game.heat[0, 0] == 2

        # Nach 29 Minuten: noch keine Abkühlung
        assert await game.cool_down(now=t0 + timedelta(minutes=29)) == []
        # Nach 31 Minuten: -1 pro Durchlauf, nie unter 0
        changes = await game.cool_down(now=t0 + timedelta(minutes=31))
        assert [(c.zx, c.zy, c.heat) for c in changes] == [(0, 0, 1)]
        await game.cool_down(now=t0 + timedelta(minutes=32))
        assert game.heat[0, 0] == 0
        assert await game.cool_down(now=t0 + timedelta(minutes=33)) == []
        assert game.heat[0, 0] == 0

    run(scenario())


def test_state_survives_restart(tmp_path):
    settings = make_settings(tmp_path)

    async def paint_some():
        g = Game(settings)
        g.open()
        uid = await user_with(g, "a", 100)
        await g.paint(uid, 10, 20, 3)
        await g.paint(uid, 10, 20, 2)
        g.close()

    run(paint_some())
    g = Game(settings)
    g.open()
    assert g.canvas[20, 10] == 2
    assert g.heat[1, 0] == 1
    g.close()


def test_topup_idempotent_and_capped(game):
    async def scenario():
        uid = await game.get_or_create_user("a")
        r1 = await game.credit_topup("fake", "pay_1", uid, 1000)
        r2 = await game.credit_topup("fake", "pay_1", uid, 1000)  # doppelter Webhook
        assert (r1.status, r2.status) == ("paid", "duplicate")
        assert game.get_user(uid)["balance_cents"] == 1000
        await game.credit_topup("fake", "pay_2", uid, 1000)
        r3 = await game.credit_topup("fake", "pay_3", uid, 1000)  # würde 30 € ergeben
        assert r3.status == "refund_needed"
        assert game.get_user(uid)["balance_cents"] == 2000

    run(scenario())


def test_rate_limiter_sliding_window():
    t = [0.0]
    lim = SlidingWindowLimiter(5, 1.0, clock=lambda: t[0])
    assert all(lim.allow("u") for _ in range(5))
    assert not lim.allow("u")
    assert lim.allow("other")
    t[0] = 0.999
    assert not lim.allow("u")
    t[0] = 1.001
    assert lim.allow("u")


def test_event_phases(tmp_path):
    from datetime import time

    s = make_settings(
        tmp_path,
        event_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        event_end=datetime(2026, 7, 10, tzinfo=timezone.utc),
        night_pause=(time(0, 0), time(8, 0)),
    )
    assert event_phase(s, datetime(2026, 6, 30, tzinfo=timezone.utc)) == BEFORE
    assert event_phase(s, datetime(2026, 7, 10, tzinfo=timezone.utc)) == ENDED
    # 23:30 UTC im Sommer = 01:30 in Berlin → Nachtpause
    assert event_phase(s, datetime(2026, 7, 2, 23, 30, tzinfo=timezone.utc)) == NIGHT_PAUSE
    # 06:30 UTC = 08:30 in Berlin → läuft
    assert event_phase(s, datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)) == RUNNING
