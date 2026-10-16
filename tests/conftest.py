from dataclasses import replace

import pytest

from quantbot.config import Config
from quantbot.execution import PaperEngine
from quantbot.models import Quote, Snapshot
from quantbot.storage import Store

NOW = 1700000000.0
CANDLE = 1699995600000


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path)


@pytest.fixture
def engine(config):
    store = Store(config)
    engine = PaperEngine(config, store)
    yield engine
    store.close()


def quote(price=100, now=NOW):
    return Quote(price, now)


def snapshot(price=100, atr=5, side="LONG", candle=CANDLE, **values):
    base = Snapshot(
        candle, NOW, price, 100, 95 if side == "LONG" else 105, 90 if side == "LONG" else 110, 50, atr, 2
    )
    return replace(base, **values)


def decision(action="BUY", confidence=0.9, scale=False, regime="TRENDING_BULL"):
    return {
        "action": action,
        "confidence": confidence,
        "scale_in_signal": scale,
        "market_regime": regime,
        "thesis": "test",
        "copilot_commentary": "test",
    }


def apply(engine, value=None, data=None, price=100, **values):
    data = data or snapshot(price)
    options = dict(
        now=NOW, requested_at=NOW, revision=engine.state["position_revision"], latest_candle=data.candle_id
    )
    options.update(values)
    return engine.apply_decision(value or decision(), data, quote(price), **options)


def seed(engine, side="LONG", **values):
    state = engine.snapshot()
    state["portfolio"].update(
        side=side,
        position_units=1.0,
        entry_price=100.0,
        stop_loss=90.0 if side == "LONG" else 110.0,
        take_profit=120.0 if side == "LONG" else 80.0,
        tranches=1,
    )
    state["portfolio"].update(values)
    engine._commit(state, [])
