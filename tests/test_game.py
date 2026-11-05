"""Tests direkt am Spielkern, ohne HTTP."""

import asyncio
from datetime import datetime, time, timedelta, timezone

import numpy as np
import pytest

from pixelwar.event import BEFORE, ENDED, NIGHT_PAUSE, RUNNING, event_phase
from pixelwar.game import Game, GameError
from pixelwar.pricing import PriceParams, price_array, price_cents
from pixelwar.ratelimit import SlidingWindowLimiter

from .conftest import make_settings

P = PriceParams()


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
    assert price_cents(0, False, P) == 2
    assert price_cents(1, False, P) == 2  # 2.3 → 2
    assert price_cents(5, False, P) == 4  # 4.02 → 4
    assert price_cents(10, False, P) == 8  # 8.09 → 8
    # Leere Pixel kosten immer den Grundpreis, egal wie heiß.
    assert price_cents(10, True, P) == 2
    heat = np.arange(60, dtype=float)
    arr = price_array(heat, np.zeros(60, bool), P)
    assert [price_cents(h, False, P) for h in range(60)] == arr.tolist()


def test_empty_keeps_heat_overpaint_raises_it_and_infects_neighbors(game):
    async def scenario():
        uid = await user_with(game, "a", 1000)
        r1 = await game.paint(uid, 5, 5, 1)
        assert r1.price_cents == 2 and game.heat[5, 5] == 0
        # Übermalen: heat + 1 auf dem Pixel, + 0.02 auf den 8 Nachbarn
        await game.paint(uid, 5, 5, 2)
        assert game.heat[5, 5] == 1
        for nx, ny in [(4, 4), (5, 4), (6, 4), (4, 5), (6, 5), (4, 6), (5, 6), (6, 6)]:
            assert game.heat[ny, nx] == pytest.approx(0.02)
        assert game.heat[5, 7] == 0  # zwei Pixel weiter: nichts
        # Leere Nachbarn bleiben beim Grundpreis
        assert game.prices[4, 4] == 2
        return uid

    uid = run(scenario())
    assert game.get_user(uid)["balance_cents"] == 1000 - 2 - 2


def test_contagion_on_canvas_edge(game):
    async def scenario():
        uid = await user_with(game, "a", 100)
        await game.paint(uid, 0, 0, 1)
        await game.paint(uid, 0, 0, 2)  # Ecke: nur 3 Nachbarn

    run(scenario())
    assert np.count_nonzero(game.heat) == 4


def test_price_rises_with_heat(game):
    async def scenario():
        uid = await user_with(game, "a", 2500)
        await game.paint(uid, 0, 0, 1)
        prices = []
        for i in range(12):
            r = await game.paint(uid, 0, 0, 2 + i % 2)
            prices.append(r.price_cents)
        return prices

    prices = run(scenario())
    assert prices == [price_cents(h, False, P) for h in range(12)]
    assert prices[-1] > prices[0]


def test_empty_pixel_with_contagion_heat_costs_base_then_keeps_heat(game):
    async def scenario():
        uid = await user_with(game, "a", 2500)
        await game.set_params(PriceParams(contagion=1.0))
        await game.paint(uid, 10, 10, 1)
        for i in range(4):
            await game.paint(uid, 10, 10, 2 + i % 2)
        # (11, 10) ist leer, hat aber 4 × 1.0 heat angesammelt
        assert game.heat[10, 11] == pytest.approx(4.0)
        r = await game.paint(uid, 11, 10, 1)
        assert r.price_cents == 2  # trotzdem Grundpreis
        assert game.heat[10, 11] == pytest.approx(4.0)  # heat bleibt erhalten
        assert game.prices[10, 11] == 3  # erst jetzt zählt die Hitze: 2 * 1.15 ** 4 = 3.498 → 3

    run(scenario())


def test_price_changes_reported_only_when_rounded_price_changes(game):
    async def scenario():
        uid = await user_with(game, "a", 2500)
        await game.paint(uid, 0, 0, 1)
        r = await game.paint(uid, 0, 0, 2)  # heat 0 → 1: Preis 2 → 2, keine Änderung
        assert r.price_changes == []
        r = await game.paint(uid, 0, 0, 3)  # heat 1 → 2: Preis 2 → 3 (2.645)
        assert r.price_changes == [(0, 0, 3)]

    run(scenario())


def test_cannot_go_negative_even_with_parallel_clicks(game):
    """Der Doppelklick-Fall: viele gleichzeitige Anfragen, Guthaben reicht nur für wenige."""

    async def scenario():
        uid = await user_with(game, "a", 10)  # reicht für genau 5 Pixel à 2 ct
        tasks = [game.paint(uid, x, 0, 1) for x in range(20)]
        return uid, await asyncio.gather(*tasks, return_exceptions=True)

    uid, results = run(scenario())
    ok = [r for r in results if not isinstance(r, Exception)]
    failed = [r for r in results if isinstance(r, GameError)]
    assert len(ok) == 5 and len(failed) == 15
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
            await game.paint(uid, 0, 0, 1, max_price_cents=2)
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


def test_cooldown_one_step_per_period(game):
    async def scenario():
        uid = await user_with(game, "a", 1000)
        t0 = datetime.now(timezone.utc)
        await game.paint(uid, 0, 0, 1, now=t0)
        await game.paint(uid, 0, 0, 2, now=t0)
        await game.paint(uid, 0, 0, 3, now=t0)
        await game.paint(uid, 0, 0, 1, now=t0)
        assert game.heat[0, 0] == 3

        await game.cool_down(now=t0 + timedelta(minutes=29))
        assert game.heat[0, 0] == 3
        await game.cool_down(now=t0 + timedelta(minutes=30))
        assert game.heat[0, 0] == 2
        # Eine Minute später: noch keine zweite Stufe – erst nach weiteren 30 min
        await game.cool_down(now=t0 + timedelta(minutes=31))
        assert game.heat[0, 0] == 2
        await game.cool_down(now=t0 + timedelta(minutes=60))
        assert game.heat[0, 0] == 1
        # Nachbarn (0.06) fallen direkt auf 0, nie darunter
        assert game.heat[0, 1] == 0
        await game.cool_down(now=t0 + timedelta(minutes=90))
        await game.cool_down(now=t0 + timedelta(minutes=120))
        assert game.heat[0, 0] == 0

    run(scenario())
    # Aufgeräumt: leere Pixel ohne heat haben keine Zeile mehr
    rows = game.reader.execute("SELECT COUNT(*) FROM pixels").fetchone()[0]
    assert rows == 1


def test_paint_resets_cooldown_clock(game):
    async def scenario():
        uid = await user_with(game, "a", 1000)
        t0 = datetime.now(timezone.utc)
        await game.paint(uid, 0, 0, 1, now=t0)
        await game.paint(uid, 0, 0, 2, now=t0)
        await game.paint(uid, 0, 0, 3, now=t0 + timedelta(minutes=20))
        await game.cool_down(now=t0 + timedelta(minutes=35))
        assert game.heat[0, 0] == 2  # letzte Bemalung erst 15 min her

    run(scenario())


def test_runtime_params_apply_immediately_and_persist(tmp_path):
    settings = make_settings(tmp_path)

    async def scenario():
        g = Game(settings)
        g.open()
        uid = await user_with(g, "a", 1000)
        await g.paint(uid, 0, 0, 1)
        await g.paint(uid, 0, 0, 2)
        await g.set_params(PriceParams(base_cents=5, factor=2.0, contagion=0.5, cooldown_minutes=10))
        assert g.prices[0, 0] == 10  # 5 * 2 ** 1
        assert g.prices[200, 200] == 5  # leeres Pixel: neuer Grundpreis
        r = await g.paint(uid, 0, 0, 1)
        assert r.price_cents == 10
        assert g.heat[1, 1] == pytest.approx(0.02 + 0.5)  # neue Ansteckung
        with pytest.raises(ValueError):
            await g.set_params(PriceParams(factor=115))
        g.close()

    run(scenario())
    g = Game(settings)
    g.open()
    assert g.params == PriceParams(base_cents=5, factor=2.0, contagion=0.5, cooldown_minutes=10)
    assert g.prices[0, 0] == 20  # 5 * 2 ** 2, nach Neustart aus der DB
    g.close()


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
    assert g.heat[20, 10] == 1
    assert g.heat[21, 11] == pytest.approx(0.02)
    g.close()


def test_pixel_info(game):
    async def scenario():
        uid = await user_with(game, "alice", 100)
        await game.paint(uid, 3, 3, 1)
        await game.paint(uid, 3, 3, 2)

    run(scenario())
    info = game.pixel_info(3, 3)
    assert info["user"] == "alice" and info["overpaint_count"] == 1 and info["color_index"] == 2
    assert info["painted_at"].endswith("Z")
    empty = game.pixel_info(3, 4)  # nur angesteckt, nie bemalt
    assert empty["user"] is None and empty["painted_at"] is None and empty["price_cents"] == 2


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
