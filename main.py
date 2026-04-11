import sys, os, time, subprocess
from mininet.log import setLogLevel

from topoBuilder import fatTreeBuilder
from flowGenerator import flowGenerator
from packet_captuer import CapturePoint, PacketCapturer

"""
실행 방법:
    sudo python3 main.py <port> <flowsPerPair> [k]

인수:
    flowsPerPair : src-dst 쌍 당 생성할 플로우 수
    k            : fat-tree 파라미터 (기본값 4)

예시:
    sudo python3 main.py 100
    sudo python3 main.py 100 4
"""


def disable_interface_offloads(network):
    """패킷 캡처 왜곡을 줄이기 위해 Mininet 인터페이스의 offload를 끈다."""
    for node in list(network.hosts) + list(network.switches):
        for intf in node.intfList():
            if not intf.name or intf.name == 'lo':
                continue
            node.cmd(
                'ethtool -K %s gro off gso off tso off rx off tx off >/dev/null 2>&1 || true'
                % intf.name
            )

if __name__ == '__main__':

    if len(sys.argv) < 2:
        print('Usage: sudo python3 %s <flowsPerPair>' % sys.argv[0])
        sys.exit(1)

    flowsPerPair = int(sys.argv[1])
    k       = 4    # fat-tree 파라미터 고정
    DST_PORT = 5001 # 트래픽을 받는 HOST 포트 번호 5001로 고정

    setLogLevel('info')

    # TrafficGenerator 컴파일
    os.system('cd TrafficGenerator && make && cd -')

    # Ryu 컨트롤러 백그라운드 실행
    print('[*] Ryu 컨트롤러 시작 중...')
    ryu_proc = subprocess.Popen([
        'docker', 'run', '--rm', '--network', 'host',
        '-v', os.path.abspath(os.path.dirname(__file__)) + ':/app',
        'ryu-py3',
        'ryu-manager', '/app/main_controller.py'
    ])
    time.sleep(3)  # Ryu가 준비될 때까지 대기

    try:
        # Fat-tree 토폴로지 생성 및 시작
        print('[*] Fat-tree 토폴로지 생성 중... (k=%d)' % k)
        network = fatTreeBuilder(k=k)
        network.start()
        disable_interface_offloads(network)
        print('[*] 인터페이스 offload 비활성화 완료')
        print('[*] 토폴로지 생성 완료 (스위치 %d개, 호스트 %d개)' % (
            len(network.switches), len(network.hosts)))

        # 스위치들이 Ryu에 연결되고 룰이 설치될 때까지 대기
        print('[*] 스위치 룰 설치 대기 중...')
        time.sleep(3)
        print('[*] 룰 설치 완료 확인')

        # 모든 edge 스위치에 캡처 지점 생성 (host-facing 인터페이스)
        capture_points = []
        for edge_name in network.topo.edge_switches:
            edge_node = network[edge_name]
            # edge_name 형식: edge_p{pod}_e{edge_idx}
            pod      = edge_name.split('_p')[1].split('_e')[0]
            edge_idx = edge_name.split('_e')[1]
            for i in range(1, k // 2 + 1):
                iface = '%s-eth%d' % (edge_name, i)
                host_ip = '10.%s.%s.%d' % (pod, edge_idx, i)
                capture_points.append(CapturePoint(node=edge_node, interface=iface, src_ips=(host_ip,)))
        print('[*] 캡처 지점 %d개 생성 완료 (edge 스위치 %d개 × 인터페이스 %d개)' % (
            len(capture_points), len(network.topo.edge_switches), k // 2))

        # PacketCapturer 시작
        capturer = PacketCapturer(
            capture_points=capture_points,
            output_dir='captured_packet',
            server_port=DST_PORT,
        )
        capturer.start()
        print('[*] 패킷 캡처 시작 (저장 위치: captured_packet/)')

        # 트래픽 생성 (pod0,1=src → pod2,3=dst)
        print('[*] 트래픽 생성 시작...')
        flowGenerator(network, DST_PORT, flowsPerPair)

        time.sleep(1)

        capturer.stop()
        print('[*] 패킷 캡처 중단')

        # 결과 분석
        result_script = 'TrafficGenerator/src/script/result.py'
        for i in range(2 * (k // 2) ** 2):  # src 수 = pod0+pod1 호스트 수 = 2*(k/2)^2
            flows_file = 'results/flows_%s.txt' % i
            if os.path.exists(flows_file):
                print('[*] 결과 분석 중... (%s)' % flows_file)
                os.system('python3 %s %s' % (result_script, flows_file))
            else:
                print('[!] %s 없음 - 결과를 분석할 수 없습니다.' % flows_file)

    finally:
        # 정리
        print('[*] 네트워크 종료 중...')
        network.stop()
        ryu_proc.terminate()
        os.system('mn -c')
        os.system('pkill -KILL tshark')
