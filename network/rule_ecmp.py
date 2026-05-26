# network/rule_ecmp.py

def install_rules(datapath, logger, k=4):
    """
    스위치가 컨트롤러에 처음 연결될 때 호출되는 함수.
    DPID로 스위치 타입(Core/Agg/Edge)을 식별하고 ECMP 규칙을 설치한다.

    DPID 인코딩 (topoBuilder_lb.py와 일치):
        core : 1 ~ (k/2)^2
        agg  : 101 ~ 100 + k*(k/2)
        edge : 201 ~ 200 + k*(k/2)

    포트 번호 약속 (링크 추가 순서 기준):
        Edge switch:
            port 1 .. k/2       : 호스트 (port h+1 for host h)
            port k/2+1 .. k     : agg 업링크 (port k/2+a+1 for agg a)
        Aggregation switch:
            port 1 .. k/2       : edge 다운링크 (port e+1 for edge e)
            port k/2+1 .. k     : core 업링크 (port k/2+c+1 for core c)
        Core switch:
            port 1 .. k         : 각 pod (port pod+1 for pod)

    IP 체계: 10.{pod}.{edge}.{host+1}
    """

    dpid    = datapath.id
    ofproto = datapath.ofproto
    parser  = datapath.ofproto_parser
    half    = k // 2

    # ------------------------------------------------------------------
    # 헬퍼: 플로우 규칙 1개 설치
    # ------------------------------------------------------------------
    def add_flow(priority, match, actions):
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                 match=match, instructions=inst)
        datapath.send_msg(mod)

    # ------------------------------------------------------------------
    # 헬퍼: ECMP SELECT 그룹 설치
    # ------------------------------------------------------------------
    def add_ecmp_group(group_id, ports):
        buckets = []
        for port in ports:
            actions = [parser.OFPActionOutput(port)]
            buckets.append(parser.OFPBucket(weight=1,
                                            watch_port=port,
                                            watch_group=ofproto.OFPG_ANY,
                                            actions=actions))
        mod = parser.OFPGroupMod(datapath=datapath,
                                 command=ofproto.OFPGC_ADD,
                                 type_=ofproto.OFPGT_SELECT,
                                 group_id=group_id,
                                 buckets=buckets)
        datapath.send_msg(mod)

    # ------------------------------------------------------------------
    # DPID 범위로 스위치 타입 판별
    # ------------------------------------------------------------------
    num_core   = half * half          # (k/2)^2
    num_pod_sw = k * half             # k*(k/2)

    # ── Core 스위치 ────────────────────────────────────────────────────
    if 1 <= dpid <= num_core:
        # logger.info("[ECMP] Core  dpid=%d", dpid)

        # 목적지 pod 서브넷(10.pod.0.0/16) → 해당 pod 방향 포트(pod+1)로 포워딩
        for pod in range(k):
            dst = ('10.%d.0.0' % pod, '255.255.0.0')
            match   = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst)
            actions = [parser.OFPActionOutput(pod + 1)]
            add_flow(priority=100, match=match, actions=actions)

    # ── Aggregation 스위치 ─────────────────────────────────────────────
    elif 101 <= dpid <= 100 + num_pod_sw:
        # logger.info("[ECMP] Agg   dpid=%d", dpid)

        pod = (dpid - 101) // half   # 자신이 속한 pod 번호

        # 업링크 포트: port half+1 .. k  →  ECMP 그룹으로 묶기
        uplink_ports = list(range(half + 1, k + 1))
        add_ecmp_group(group_id=1, ports=uplink_ports)

        # 다운링크: 같은 pod 내 edge 서브넷(10.pod.e.0/24) → edge 포트(e+1)
        for e in range(half):
            dst     = ('10.%d.%d.0' % (pod, e), '255.255.255.0')
            match   = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst)
            actions = [parser.OFPActionOutput(e + 1)]
            add_flow(priority=200, match=match, actions=actions)

        # 업링크: 다른 pod로 가는 트래픽 → ECMP 그룹
        match   = parser.OFPMatch(eth_type=0x0800)
        actions = [parser.OFPActionGroup(group_id=1)]
        add_flow(priority=100, match=match, actions=actions)

    # ── Edge 스위치 ────────────────────────────────────────────────────
    elif 201 <= dpid <= 200 + num_pod_sw:
        # logger.info("[ECMP] Edge  dpid=%d", dpid)

        pod      = (dpid - 201) // half   # 자신이 속한 pod 번호
        edge_idx = (dpid - 201) % half    # pod 내 edge 인덱스

        # 업링크 포트: port half+1 .. k  →  ECMP 그룹으로 묶기
        uplink_ports = list(range(half + 1, k + 1))
        add_ecmp_group(group_id=1, ports=uplink_ports)

        # 다운링크: 직접 연결된 호스트 IP(10.pod.edge.h+1) → host 포트(h+1)
        for h in range(half):
            host_ip = '10.%d.%d.%d' % (pod, edge_idx, h + 1)
            match   = parser.OFPMatch(eth_type=0x0800, ipv4_dst=host_ip)
            actions = [parser.OFPActionOutput(h + 1)]
            add_flow(priority=200, match=match, actions=actions)

        # 업링크: 로컬 호스트가 아닌 트래픽 → ECMP 그룹
        match   = parser.OFPMatch(eth_type=0x0800)
        actions = [parser.OFPActionGroup(group_id=1)]
        add_flow(priority=100, match=match, actions=actions)

    else:
        logger.warning("[ECMP] 알 수 없는 DPID: %d (k=%d)", dpid, k)