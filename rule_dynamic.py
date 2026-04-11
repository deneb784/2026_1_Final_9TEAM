# rule_dynamic.py

def install_rules(datapath, logger, k=4):
    """
    스위치가 컨트롤러에 처음 연결될 때 호출되는 함수.
    동적 라우팅 룰을 설치한다. (구현 예정)

    DPID 인코딩 (topoBuilder.py와 일치):
        core : 1 ~ (k/2)^2
        agg  : 101 ~ 100 + k*(k/2)
        edge : 201 ~ 200 + k*(k/2)
    """

    dpid    = datapath.id
    ofproto = datapath.ofproto
    parser  = datapath.ofproto_parser
    half    = k // 2

    def add_flow(priority, match, actions):
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                 match=match, instructions=inst)
        datapath.send_msg(mod)

    num_core   = half * half
    num_pod_sw = k * half

    if 1 <= dpid <= num_core:
        logger.info("[Dynamic] Core  dpid=%d", dpid)
        # TODO: Core 스위치 룰 구현

    elif 101 <= dpid <= 100 + num_pod_sw:
        logger.info("[Dynamic] Agg   dpid=%d", dpid)
        # TODO: Aggregation 스위치 룰 구현

    elif 201 <= dpid <= 200 + num_pod_sw:
        logger.info("[Dynamic] Edge  dpid=%d", dpid)
        # TODO: Edge 스위치 룰 구현

    else:
        logger.warning("[Dynamic] 알 수 없는 DPID: %d (k=%d)", dpid, k)
