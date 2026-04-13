from mininet.net import Mininet
from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import RemoteController, OVSSwitch

def fatTreeBuilder(k=4):
    """
    Fat-Tree 네트워크 토폴로지를 생성하여 Mininet 네트워크 객체를 반환합니다.
    
    구조 (k=4 기준):
        - Core 계층: 4개의 코어 스위치
        - Pod 계층 (4개): 각 Pod 내에 2개의 Aggregation, 2개의 Edge 스위치
        - Host 계층: 각 Edge 스위치마다 2개의 호스트 (총 16개)
        
    링크 대역폭:
        - Host <-> Edge: 100 Mbps
        - Edge <-> Aggregation: 100 Mbps
        - Aggregation <-> Core: 100 Mbps
    """

    class FatTreeTopo(Topo):
        def __init__(self, k):
            Topo.__init__(self)

            self.k = k
            self.core_switches = []
            self.agg_switches = []
            self.edge_switches = []
            self.host_list = []

            # 1. Core 스위치 생성: (k/2)^2 개
            # DPID 인코딩: core → 1 ~ (k/2)^2
            num_core = (k // 2) ** 2
            for i in range(1, num_core + 1):
                self.core_switches.append(
                    self.addSwitch('core%s' % i,
                                   dpid='%016x' % i,
                                   protocols='OpenFlow13',
                                   failMode='secure')
                )

            # Core 스위치를 2차원 배열(그룹)로 구성하여 추후 Aggregation과 연결하기 쉽게 만듦
            # core_matrix[i][j] 구조로 생성
            core_matrix = []
            c_idx = 0
            for i in range(k // 2):
                row = []
                for j in range(k // 2):
                    row.append(self.core_switches[c_idx])
                    c_idx += 1
                core_matrix.append(row)

            # 2. Pod 생성 (Aggregation, Edge, Host)
            num_pods = k
            sw_per_pod = k // 2
            hosts_per_edge = k // 2

            host_counter = 1

            for p in range(num_pods):
                pod_agg = []
                pod_edge = []

                # Pod 내 Aggregation 스위치 생성
                # DPID 인코딩: agg → 100 + pod*(k/2) + agg_idx + 1
                for a in range(sw_per_pod):
                    agg_name = 'agg_p%s_a%s' % (p, a)
                    agg_dpid = 100 + p * sw_per_pod + a + 1
                    pod_agg.append(self.addSwitch(agg_name,
                                                  dpid='%016x' % agg_dpid,
                                                  protocols='OpenFlow13',
                                                  failMode='secure'))
                    self.agg_switches.append(agg_name)

                # Pod 내 Edge 스위치 생성 및 Host 연결
                # DPID 인코딩: edge → 200 + pod*(k/2) + edge_idx + 1
                for e in range(sw_per_pod):
                    edge_name = 'edge_p%s_e%s' % (p, e)
                    edge_dpid = 200 + p * sw_per_pod + e + 1
                    pod_edge.append(self.addSwitch(edge_name,
                                                   dpid='%016x' % edge_dpid,
                                                   protocols='OpenFlow13',
                                                   failMode='secure'))
                    self.edge_switches.append(edge_name)

                    # Edge 스위치 하위에 Host 생성 및 연결
                    # IP 체계: 10.{pod}.{edge}.{host+1} → pod 기반 라우팅 가능
                    for h in range(hosts_per_edge):
                        host_name = 'h%s' % host_counter
                        mac_addr = '00:00:%02x:%02x:00:%02x' % (p, e, h + 1)
                        ip_addr = '10.%d.%d.%d' % (p, e, h + 1)

                        host = self.addHost(host_name, mac=mac_addr, ip=ip_addr)
                        self.host_list.append(host)

                        # Host <-> Edge 링크 (100 Mbps)
                        self.addLink(host, edge_name, bw=100)
                        host_counter += 1

                # 3. Pod 내부 스위치 간 연결: Aggregation <-> Edge (Full Mesh)
                for agg in pod_agg:
                    for edge in pod_edge:
                        self.addLink(agg, edge, bw=100)

                # 4. Pod <-> Core 연결: Aggregation <-> Core
                # 각 Pod의 i번째 Aggregation 스위치는 core_matrix의 i번째 행에 있는 모든 Core 스위치와 연결됨
                for a in range(sw_per_pod):
                    for c in range(k // 2):
                        core_sw = core_matrix[a][c]
                        self.addLink(pod_agg[a], core_sw, bw=100)

    # 토폴로지 객체 생성
    topology = FatTreeTopo(k=k)
    
    # "c0" : Controller 이름
    # ip='127.0.0.1' : localhost(mininet과 같은 곳)에서 실행
    # port=6653 : 오픈플로우(OpenFlow) 프로토콜 사용 포트
    # "c0" 이라는 Ryu Controller 생성 
    ryu_controller = RemoteController('c0', ip='127.0.0.1', port=6653)
    network = Mininet(topo=topology, controller=ryu_controller, link=TCLink, switch=OVSSwitch,
                      autoStaticArp=True)

    return network
