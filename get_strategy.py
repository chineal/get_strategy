import json
from queue import Queue
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
#from pprint import pprint

from vnpy.trader.constant import Direction, Offset
import time
import threading

class MyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        #print(urlparse(self.path))
        parse = urlparse(self.path)

        if parse.path == '/shutdown':
            self.server.main.running = False

        elif parse.path == '/vnpy':
            query = parse_qs(parse.query)
            flag = query.get('flag', [''])[0]
            sign = query.get('sign', [''])[0]
            key = query.get('key', [''])[0]
            stamp = query.get('stamp', [''])[0]
            self.server.main.on_get(flag, sign, key, stamp)

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write('ok'.encode('utf-8'))

class MyServer(HTTPServer):
    def __init__(self, main):
        self.timeout = 1
        super().__init__(('0.0.0.0', main.port), MyHandler)
        self.main = main

def update_config(config):
    with open("c:\get_strategy_config.json", 'w') as json_file:
        json.dump(config, json_file, indent=4)

class GetStrategy(CtaTemplate):
    running: bool = False
    bg: BarGenerator | None = None
    port: int = 8123
    count: int = 1
    sgin: int = -1
    target: int = 0
    bid: float = 0.0
    ask: float = 0.0
    
    parameters = ["port", "count", "sgin", "target", "bid", "ask"]

    _server: MyServer | None = None
    _server_thread: threading.Thread | None = None
    _checker_thread: threading.Thread | None = None

    _strategy: str | None = None
    _symbol: str | None = None
    _state1: int = 0
    _state2: int = 0
    _targets: Queue | None = None
    _reopen: int = -1
    _buy_orderids: list | None = None
    _sell_orderids: list | None = None
    _short_orderids: list | None = None
    _cover_orderids: list | None = None
    #_cancel_count: int = 0

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # K线合成器：从Tick合成分钟K线用
        self.bg = BarGenerator(self.on_bar)
        # 时间序列容器：计算技术指标用
        self.am = ArrayManager()
        
        self._strategy = strategy_name
        self._symbol = vt_symbol

    def __del__(self):
        self.running = False

    def on_init(self):
        # Callback when strategy is inited.
        self.write_log('on_init:20240820 strategy:%s symbol:%s port:%d' % (self._strategy, self._symbol, self.port))
        # 加载10天的历史数据用于初始化回放
        self.load_bar(10)

    def on_start(self):
        # Callback when strategy is started.p
        self.running = True
        self._targets = Queue()
        self._server = MyServer(self)
        self._buy_orderids = []
        self._sell_orderids = []
        self._short_orderids = []
        self._cover_orderids = []
        
        self._server_thread = threading.Thread(target=self.server_run)
        self._server_thread.deamon = True
        self._server_thread.start()

        self._checker_thread = threading.Thread(target=self.check_run)
        self._checker_thread.deamon = True
        self._checker_thread.start()

        if self.target == 0:
            self.target = self.pos

        if self.target > 0:
            self._state1 = 1
        if self.target < 0:
            self._state2 = 1

        self.update_config(True)
        self.write_log('on_start')
        self.put_event()

    def on_stop(self):
        # Callback when strategy is stopped.
        self.write_log('on_stop')
        self._targets.queue.clear()
        self.running = False
        self.put_event()
    
    def on_tick(self, tick: TickData):
        # Callback of new tick data update.
        self.bid = tick.bid_price_1
        self.ask = tick.ask_price_1
        self.put_event()
    
    def on_bar(self, bar: BarData):
        # Callback of new bar data update.
        self.write_log('on_bar')
        self.bg.update_bar(bar)
        self.put_event()

    def on_trade(self, trade: TradeData):
        #pprint(trade)
        if trade.offset == Offset.OPEN:
            if trade.direction == Direction.LONG:
                self._state1 = 1
                self.target = 1
            else:
                self._state2 = 1
                self.target = -1
        else:
            self.sgin = -1
            if trade.direction == Direction.LONG:
                self._state2 = 0
            else:
                self._state1 = 0
            self.target = 0
        self.put_event()
        self.write_log('on_trade:%s 多:%d 空:%d' % (trade.orderid, self._state1, self._state2))

        order_id = "CTP.{}".format(trade.orderid)
        for buf_orderids in [self._buy_orderids, self._sell_orderids, self._short_orderids,self._cover_orderids]:
            if order_id in buf_orderids:
                buf_orderids.remove(order_id)
                self.write_log("finish order id:%s remain:%s" % (order_id, buf_orderids))

        self.update_config()
        
    def on_order(self, order: OrderData):
        print('on_order')
        order_id = "CTP.{}".format(order.orderid)
        print("update order id:%s status:%s" % (order_id, order.status))
        if order.status not in [order.status.CANCELLED, order.status.REJECTED]:
            return
        
        if order.status == order.status.REJECTED:
            if self.target == 0:
                self._state1 = 0
                self._state2 = 0
            else:
                if self.target == 1 and self._state2 == 1:
                    self._state2 = 0
                elif self.target == -1 and self._state1 == 1:
                    self._state1 = 0
            self.put_event()

        for buf_orderids in [self._buy_orderids, self._sell_orderids, self._short_orderids,self._cover_orderids]:
            if order_id in buf_orderids:
                buf_orderids.remove(order_id)
                self.write_log("remove order id:%s remain:%s" % (order_id, buf_orderids))
        
    def on_stop_order(self, stop: StopOrder):
        self.write_log('on_stop_order')

    def on_get(self, flag, sign, key, stamp):
        self.write_log('请求:%s, %s:%s, state1:%s, state2:%s' % (self._symbol, self.sgin, self.target, self._state1, self._state2))

        if key != 0 and int(self.sgin) != -1 and int(sign) != int(self.sgin):
            self.write_log('请求冲突:%s, %s:%s, %s:%s' % (self._symbol, self.sgin, self.target, sign, key))
            return

        if key == '1' and self._state1 == 0:
            self.write_log('请求买开:%s, 周期码:%s, %s元' % (self._symbol, sign, self.ask))
            #self._cancel_count = 0
            self._targets.put(1)
            self.sgin = int(sign)
            self.put_event()
            return
        
        if key == '2' and self._state2 == 0:
            self.write_log('请求卖开:%s, 周期码:%s, %s元' % (self._symbol, sign, self.bid))
            #self._cancel_count = 0
            self._targets.put(-1)
            self.sgin = int(sign)
            self.put_event()
            return
        
        if key == '9':
            self.write_log('请求卖开:%s, 周期码:%s, %s元' % (self._symbol, sign, self.bid))
            self._targets.put(9)
            self.sgin = int(sign)
            self.put_event()
            return
        
        if key == '0':
            self.write_log('全平:%s, %s元, %s元' % (self._symbol, self.ask, self.bid))
        elif (key == '3' or key == '5') and self.target != 0:
            self.write_log('请求买平:%s, 周期码:%s, %s元' % (self._symbol, sign, self.ask))
        elif (key == '4' or key == '6') and self.target != 0:
            self.write_log('请求卖平:%s, 周期码:%s, %s元' % (self._symbol, sign, self.bid))
        else:
            return
        #self._cancel_count = 0
        self._targets.put(0)
        self.sgin = int(sign)
        self.put_event()

    def server_run(self):
        self.write_log('my strategy server start run')
        while self.running:
            self._server.handle_request()
        del self._server

    def check_run(self):
        self.write_log('my strategy checker start run')
        while self.running:
            if self._targets.empty():
                continue
            self.target = self._targets.get()
            self._reopen = 0

            while True:
                #if self._cancel_count > 100:
                time.sleep(0.5)

                if self.target == 9:
                    self.write_log('撤销:%s, 目标:%s, 多:%s, 空:%s' 
                                   % (self._symbol, self.target, self._state1, self._state2))
                    self.my_cancel()

                if (self.target == 0 or self.target == -1) and self._state1 == 1:
                    if len(self._sell_orderids) == 0:
                        self._sell_orderids = self.sell(self.bid, self.count)
                        self.write_log('卖平:%s, %s元, 目标:%s, 多:%s, 标识:%s'
                                       % (self._symbol, self.ask, self.target, self._state1, self._sell_orderids))
                    else:
                        self.my_cancel()
                    continue

                if (self.target == 0 or self.target == 1) and self._state2 == 1:
                    if len(self._cover_orderids) == 0:
                        self._cover_orderids = self.cover(self.ask, self.count)
                        self.write_log('买平:%s, %s元, 目标:%s, 空:%s, 标识:%s'
                                       % (self._symbol, self.bid, self.target, self._state2, self._cover_orderids))
                    else:
                        self.my_cancel()
                    continue

                if self.target == 1 and self._state1 == 0 and self._reopen < 3:
                    self._reopen += 1
                    if len(self._buy_orderids) == 0:
                        self._buy_orderids = self.buy(self.ask, self.count)
                        self.write_log('买开:%s, %s元, 目标:%s, 多:%s, 标识:%s'
                                       % (self._symbol, self.bid, self.target, self._state1, self._buy_orderids))
                    else:
                        self.my_cancel()
                    continue

                if self.target == -1 and self._state2 == 0 and self._reopen < 3:
                    self._reopen += 1
                    if len(self._short_orderids) == 0:
                        self._short_orderids = self.short(self.bid, self.count)
                        self.write_log('卖开:%s, %s元, 目标:%s, 空:%s, 标识:%s'
                                       % (self._symbol, self.ask, self.target, self._state2, self._short_orderids))
                    else:
                        self.my_cancel()
                    continue

                if self._reopen >= 3 and ((
                    self.target == 1 and self._state1 == 0) or (
                        self.target == -1 and self._state2 == 0)):
                    self.write_log('开仓失败:%s, 目标:%s' % (self._symbol, self.target))
                    self.target = 0
                    self._buy_orderids = []
                    self._short_orderids = []
                    self.put_event()

                if self.target == 0 and self._state1 == 0 and self._state2 == 0:
                    self.sgin = -1
                    self.put_event()

                break

    def update_config(self, init=False):
        with open("c:\get_strategy_config.json") as json_file:
            config = json.load(json_file)
            
            if config.get(self._symbol) is None:
                config[self._symbol] = {}

            if init:
                if config[self._symbol].get("sgin") is not None:
                    self.sgin = int(config[self._symbol]['sgin'])
            else:
                config[self._symbol]['sgin'] = int(self.sgin)
                update_config(config)
    
    def my_cancel(self):
        for buf_orderids in [self._buy_orderids, self._sell_orderids, self._short_orderids,self._cover_orderids]:
            for buf_orderid in buf_orderids:
                #self._cancel_count += 1
                self.cancel_order(buf_orderid)
                self.write_log("cancel order id:%s" % buf_orderid)
                