import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("ctp_homework", Path(__file__).with_name("demo.py"))
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def make_md():
    class MD(demo.MdCallbacks, demo.FakeTransport):
        pass
    api = MD()
    api.calls = []
    api.setup({"userid": "test", "password": "test", "brokerid": "9999", "symbols": ["rb2701", "rb2701"]}, lambda *a, **k: None)
    return api


def test_failed_login_never_subscribes():
    api = make_md()
    api.onRspUserLogin({}, {"ErrorID": 3}, 1, True)
    assert not api.logged_in
    assert api.calls == []


def test_reconnect_resets_state_and_resubscribes_once():
    api = make_md()
    api.onRspUserLogin({}, {}, 1, True)
    api.onRspSubMarketData({"InstrumentID": "rb2701"}, {}, 1, True)
    api.onFrontDisconnected(4097)
    assert not api.logged_in and not api.acknowledged
    api.onFrontConnected()
    api.onRspUserLogin({}, {}, 2, True)
    assert api.calls == [("subscribe", "rb2701"), ("login", 1), ("subscribe", "rb2701")]


def test_invalid_price_is_not_a_quote():
    for value in [1.7976931348623157e308, float("nan"), float("inf"), 0, -1, None]:
        assert demo.valid_price(value) is None
    assert demo.valid_price(3200) == 3200


def test_td_disconnect_clears_login():
    td = demo.TdCallbacks()
    td.setup({}, lambda *a, **k: None)
    td.logged_in = td.connected = True
    td.onFrontDisconnected(4097)
    assert not td.logged_in and not td.connected


def test_td_authentication_failure_never_logs_in():
    class TD(demo.TdCallbacks, demo.FakeTransport):
        pass
    td = TD()
    td.calls = []
    td.setup({}, lambda *a, **k: None)
    td.onRspAuthenticate({}, {"ErrorID": 1}, 1, True)
    assert td.calls == []
