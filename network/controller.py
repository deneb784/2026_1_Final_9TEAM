from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3

# 사용할 라우팅 방식 선택
try:
    from network import rule_ecmp as routing_logic
    # from network import rule_dynamic as routing_logic
except ImportError:
    import rule_ecmp as routing_logic
    # import rule_dynamic as routing_logic

class ModularFatTreeController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ModularFatTreeController, self).__init__(*args, **kwargs)
        self.k = 4  # fat-tree 파라미터
        self.connected = 0
        # 전체 스위치 수: core (k/2)^2 + agg k*(k/2) + edge k*(k/2)
        self.total_switches = (self.k // 2) ** 2 + 2 * self.k * (self.k // 2)

    # 스위치가 처음 켜져서 컨트롤러와 연결될 때 딱 한 번 실행됨
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 외부 파일(모듈)에 정의된 라우팅 룰 설치 함수 호출
        routing_logic.install_rules(datapath, self.logger, self.k)

        self.connected += 1
        if self.connected == self.total_switches:
            self.logger.info('[*] 전체 스위치 룰 설치 완료 (%d개)', self.total_switches)
