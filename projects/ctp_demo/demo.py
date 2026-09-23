"""第二讲：底层 CTP 双通道登录和持续行情。默认前台运行，Ctrl+C 退出。

python projects/ctp_demo/demo.py --offline
python projects/ctp_demo/demo.py --duration 60
没有委托、撤单或结算确认操作。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[2]


def valid_price(value):
    """CTP 的无效双精度价格可能是 DBL_MAX，不能直接当价格显示。"""
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 < value < 1e20 else None
    except (TypeError, ValueError):
        return None


class Recorder:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.file = (directory / "events.jsonl").open("x", encoding="utf-8")
        self.lock = Lock()
        self.counts = Counter()

    def __call__(self, event, **fields):
        record = {"received_at": datetime.now().astimezone().isoformat(), "event": event, **fields}
        with self.lock:
            self.counts[event] += 1
            line = json.dumps(record, ensure_ascii=False, allow_nan=False)
            self.file.write(line + "\n")
            self.file.flush()
            print(line, flush=True)

    def finish(self, state):
        result = {"counts": dict(self.counts), **state}
        (self.directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.file.close()


class CommonCallbacks:
    """与 C++ 扩展解耦，离线测试也运行相同回调。"""
    def setup(self, config, emit, channel):
        self.config, self.emit, self.channel = config, emit, channel
        self.reqid = 0
        self.initialized = False
        self.connected = False
        self.logged_in = False
        self.login_successes = 0

    def request(self, method, payload):
        self.reqid += 1
        result = method(payload, self.reqid)
        if result != 0:
            self.emit("request_rejected_locally", channel=self.channel, code=result, reqid=self.reqid)
        return result

    def login(self):
        return self.request(self.reqUserLogin, {
            "UserID": self.config["userid"], "Password": self.config["password"],
            "BrokerID": self.config["brokerid"],
        })

    def onFrontDisconnected(self, reason):
        self.connected = self.logged_in = False
        self.emit("disconnected", channel=self.channel, reason=reason)
        # CTP 自己重连；不重复 create/init。重连回调会重新登录、订阅。

    def onRspError(self, error, reqid, last):
        self.emit("api_error", channel=self.channel, code=(error or {}).get("ErrorID"),
                  message=(error or {}).get("ErrorMsg", ""), reqid=reqid)

    def login_response(self, data, error):
        if (error or {}).get("ErrorID", 0):
            self.logged_in = False
            self.emit("login_failed", channel=self.channel, code=error["ErrorID"], message=error.get("ErrorMsg", ""))
            return False
        self.logged_in = True
        self.login_successes += 1
        self.emit("login_succeeded", channel=self.channel, trading_day=data.get("TradingDay"))
        return True

    def close(self):
        if self.initialized:
            self.exit()  # 主线程退出，不能从 CTP 自身回调线程调用 exit()。
            self.initialized = self.connected = self.logged_in = False


class MdCallbacks(CommonCallbacks):
    def setup(self, config, emit, channel="MD"):
        super().setup(config, emit, channel)
        self.symbols = list(dict.fromkeys(config["symbols"]))
        self.ticks = Counter()
        self.volumes = {}
        self.acknowledged = set()

    def connect(self, flow):
        if self.initialized:
            return
        self.createFtdcMdApi(str(flow / "Md").encode("GBK"), self.config["production_mode"])
        self.registerFront(self.config["md_address"])
        self.initialized = True
        self.init()

    def onFrontConnected(self):
        self.connected = True
        self.emit("connected", channel="MD")
        self.login()

    def onFrontDisconnected(self, reason):
        self.acknowledged.clear()
        super().onFrontDisconnected(reason)

    def onRspUserLogin(self, data, error, reqid, last):
        if self.login_response(data, error):
            for symbol in self.symbols:
                result = self.subscribeMarketData(symbol)
                self.emit("subscription_requested", symbol=symbol, code=result)

    def onRspSubMarketData(self, data, error, reqid, last):
        symbol = data.get("InstrumentID", "")
        code = (error or {}).get("ErrorID", 0)
        if not code:
            self.acknowledged.add(symbol)
        self.emit("subscription_response", symbol=symbol, code=code, message=(error or {}).get("ErrorMsg", ""))

    def onRtnDepthMarketData(self, data):
        symbol = data.get("InstrumentID", "")
        if symbol not in self.symbols:
            return
        self.ticks[symbol] += 1
        self.volumes[symbol] = data.get("Volume", 0)
        self.emit("tick", symbol=symbol, last=valid_price(data.get("LastPrice")),
                  bid=valid_price(data.get("BidPrice1")), ask=valid_price(data.get("AskPrice1")),
                  bid_volume=data.get("BidVolume1"), ask_volume=data.get("AskVolume1"),
                  volume=data.get("Volume"), open_interest=data.get("OpenInterest"),
                  trading_day=data.get("TradingDay"), action_day=data.get("ActionDay"),
                  exchange_time=f"{data.get('UpdateTime', '')}.{int(data.get('UpdateMillisec', 0)):03d}")


class TdCallbacks(CommonCallbacks):
    def setup(self, config, emit, channel="TD"):
        super().setup(config, emit, channel)

    def connect(self, flow):
        if self.initialized:
            return
        self.createFtdcTraderApi(str(flow / "Td").encode("GBK"), self.config["production_mode"])
        self.registerFront(self.config["td_address"])
        self.initialized = True
        self.init()

    def onFrontConnected(self):
        self.connected = True
        self.emit("connected", channel="TD")
        self.request(self.reqAuthenticate, {
            "UserID": self.config["userid"], "BrokerID": self.config["brokerid"],
            "AppID": self.config["appid"], "AuthCode": self.config["auth_code"],
        })

    def onRspAuthenticate(self, data, error, reqid, last):
        if (error or {}).get("ErrorID", 0):
            self.emit("authentication_failed", channel="TD", code=error["ErrorID"], message=error.get("ErrorMsg", ""))
        else:
            self.emit("authenticated", channel="TD")
            self.login()

    def onRspUserLogin(self, data, error, reqid, last):
        self.login_response(data, error)


class FakeTransport:
    """仅用于明确标记的合成回放，不联网、不模拟成交。"""
    def reqUserLogin(self, payload, reqid):
        self.calls.append(("login", reqid))
        return 0

    def subscribeMarketData(self, symbol):
        self.calls.append(("subscribe", symbol))
        return 0


def run_offline(emit):
    class ReplayMd(MdCallbacks, FakeTransport):
        pass
    api = ReplayMd()
    api.calls = []
    api.setup({"userid": "synthetic", "password": "", "brokerid": "9999", "symbols": ["DEMO"]}, emit)
    emit("synthetic_replay", note="合成行情，只验证代码流程，不是 SimNow 实测")
    api.onFrontConnected()
    api.onRspUserLogin({"TradingDay": "20260922"}, {}, 1, True)
    api.onRspSubMarketData({"InstrumentID": "DEMO"}, {}, 2, True)
    for i, price in enumerate([100, 101, 102]):
        api.onRtnDepthMarketData({"InstrumentID": "DEMO", "LastPrice": price, "BidPrice1": price - 1,
                                 "AskPrice1": price + 1, "Volume": i + 1, "OpenInterest": 100,
                                 "BidVolume1": 1, "AskVolume1": 2, "UpdateTime": "09:00:00", "UpdateMillisec": i})
    api.onFrontDisconnected(4097)
    api.onFrontConnected()
    api.onRspUserLogin({"TradingDay": "20260922"}, {}, 3, True)
    return {"mode": "SYNTHETIC_OFFLINE", "tick_counts": dict(api.ticks), "login_successes": api.login_successes,
            "resubscriptions": sum(c[0] == "subscribe" for c in api.calls)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--md-only", action="store_true")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.simnow.local.json"))
    parser.add_argument("--duration", type=float, default=0, help="0=持续运行直到 Ctrl+C")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("duration 必须 >= 0")
    out = args.output or ROOT / "reports" / ("ctp_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    emit = Recorder(out)
    if args.offline:
        emit.finish(run_offline(emit))
        return 0
    if not args.config.exists():
        emit("configuration_missing", path=str(args.config))
        emit.finish({"mode": "LIVE", "status": "CONFIGURATION_MISSING"})
        return 2
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    config["password"] = os.environ.get("SIMNOW_PASSWORD", config.get("password", ""))
    for key in ("userid", "password", "brokerid", "md_address", "td_address", "appid", "auth_code"):
        if not config.get(key):
            raise ValueError(f"配置缺失：{key}")
    if not config.get("symbols") or not isinstance(config.get("production_mode"), bool):
        raise ValueError("必须提供 symbols 和布尔型 production_mode")
    # 将 vnpy 的本地状态限定在作业目录，避免改用户 HOME。
    os.chdir(ROOT)
    (ROOT / ".vntrader").mkdir(exist_ok=True)
    from vnpy_ctp.api import MdApi, TdApi

    class LiveMd(MdCallbacks, MdApi):
        def __init__(self):
            MdApi.__init__(self)
            self.setup(config, emit)

    class LiveTd(TdCallbacks, TdApi):
        def __init__(self):
            TdApi.__init__(self)
            self.setup(config, emit)

    md = LiveMd()
    td = None if args.md_only else LiveTd()
    flow = out / "flow"
    flow.mkdir()
    start = time.monotonic()
    try:
        emit("starting", mode="LIVE", md_address=config["md_address"], symbols=config["symbols"], duration=args.duration)
        md.connect(flow)
        if td:
            td.connect(flow)
        while not args.duration or time.monotonic() - start < args.duration:
            time.sleep(1)
            elapsed = int(time.monotonic() - start)
            if elapsed % 10 == 0:
                emit("heartbeat", elapsed_seconds=elapsed, md_logged_in=md.logged_in,
                     td_logged_in=td.logged_in if td else None, tick_counts=dict(md.ticks))
    except KeyboardInterrupt:
        emit("interrupted")
    finally:
        md.close()
        if td:
            td.close()
        emit.finish({"mode": "LIVE", "status": "TICKS_RECEIVED" if md.ticks else "NO_TICKS_UNVERIFIED",
                     "md_login_successes": md.login_successes, "td_login_successes": td.login_successes if td else None,
                     "tick_counts": dict(md.ticks), "latest_session_volumes": md.volumes,
                     "elapsed_seconds": round(time.monotonic() - start, 2), "orders_sent": 0})
    return 0 if md.ticks else 2


if __name__ == "__main__":
    raise SystemExit(main())
