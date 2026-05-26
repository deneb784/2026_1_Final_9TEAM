import argparse
import shutil
import sys, os, time, subprocess
from mininet.log import setLogLevel

from topoBuilder import fatTreeBuilder
from flowGenerator import flowGenerator
from packet_captuer import CapturePoint, CombinedPacketCapturer, PacketCapturer, NodeXdpPacketCapturer
# from analyze.ecmp_verification import run_ecmp_verification

"""
실행 방법:
    sudo python3 main.py <flowsPerPair>

인수:
    flowsPerPair : src-dst 쌍 당 생성할 플로우 수

예시:
    sudo python3 main.py 100
    sudo python3 main.py 500 --run-id 001 --load-mbps 80 --seed-base 1000
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


def configure_ecmp_group_selection(network, k):
    """Ryu가 설치한 base ECMP group을 OVS hash selection 방식으로 다시 설정한다."""
    uplink_ports = list(range(k // 2 + 1, k + 1))

    def _mod_group(switch_name, basis):
        buckets = ",".join("bucket=output:%d" % port for port in uplink_ports)
        group_spec = (
            "group_id=1,type=select,"
            "selection_method=hash,"
            "selection_method_param=%d,"
            "fields(ip_src,ip_dst,tcp_src,tcp_dst),%s"
            % (basis, buckets)
        )
        subprocess.run(
            ["ovs-ofctl", "-O", "OpenFlow15", "mod-group", switch_name, group_spec],
            check=True,
        )

    for idx, edge_name in enumerate(network.topo.edge_switches):
        _mod_group(edge_name, 100 + idx)

    for idx, agg_name in enumerate(network.topo.agg_switches):
        # edge와 다른 basis를 사용해 상위 계층에서 동일 hash 결과가 반복되는 현상을 줄인다.
        _mod_group(agg_name, 1000 + idx)


def make_tree_readable(path):
    """sudo 실행 후 생성된 run 산출물을 일반 사용자 후처리에서 읽을 수 있게 한다."""
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o755)
        for dirname in dirs:
            os.chmod(os.path.join(root, dirname), 0o755)
        for filename in files:
            os.chmod(os.path.join(root, filename), 0o644)

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('flowsPerPair', type=int)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--output-root', default='runs')
    parser.add_argument('--load-mbps', type=int, default=80)
    parser.add_argument('--seed-base', type=int, default=1000)
    parser.add_argument('--cdf-file', default='FB_CDF.txt')
    parser.add_argument('--capture-mode', choices=['tshark', 'xdp', 'xdp-verify', 'none'], default='tshark')
    parser.add_argument('--xdp-mode', choices=['skb', 'native', 'hw'], default='skb')
    parser.add_argument('--feature-packet-count', type=int, default=10)
    parser.add_argument('--redis-url', default=None)
    parser.add_argument('--redis-stream', default='flow_features')
    parser.add_argument('--redis-stream-maxlen', type=int, default=None)
    parser.add_argument(
        '--publish-direction',
        choices=['src_to_dst', 'dst_to_src', 'all'],
        default='dst_to_src',
    )
    args = parser.parse_args()

    flowsPerPair = args.flowsPerPair
    k       = 4    # fat-tree 파라미터 고정
    DST_PORT = 5001 # 트래픽을 받는 HOST 포트 번호 5001로 고정

    setLogLevel('info')
    run_id = args.run_id or time.strftime('%Y%m%d_%H%M%S')
    run_dir = os.path.join(args.output_root, 'mininet_%s' % run_id)
    results_dir = os.path.join(run_dir, 'results')
    capture_dir = os.path.join(run_dir, 'captured_packet')
    runtime_capture_dir = '/tmp/capstone_captured_packet_%s' % run_id

    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(capture_dir, exist_ok=True)

    # TrafficGenerator 컴파일
    os.system('cd TrafficGenerator && make && cd -')

    # Ryu 컨트롤러 백그라운드 실행
    print('[*] Ryu 컨트롤러 시작 중...')
    network = None
    capturer = None
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

        # print('[*] OVS hash 기반 ECMP group selection 재설정 중...')
        # configure_ecmp_group_selection(network, k)
        # print('[*] OVS hash 기반 ECMP group selection 적용 완료')

        # 모든 edge 스위치에 host-facing 캡처 지점 생성
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
            # ECMP 검증용 edge uplink 캡처는 잠시 비활성화한다.
            # for i in range(k // 2 + 1, k + 1):
            #     iface = '%s-eth%d' % (edge_name, i)
            #     capture_points.append(CapturePoint(
            #         node=edge_node,
            #         interface=iface,
            #         capture_filter='tcp',
            #         output_name=iface,
            #     ))

        # ECMP 검증용 agg uplink 캡처는 잠시 비활성화한다.
        # for agg_name in network.topo.agg_switches:
        #     agg_node = network[agg_name]
        #     for i in range(k // 2 + 1, k + 1):
        #         iface = '%s-eth%d' % (agg_name, i)
        #         capture_points.append(CapturePoint(
        #             node=agg_node,
        #             interface=iface,
        #             capture_filter='tcp',
        #             output_name=iface,
        #         ))
        print(
            '[*] 캡처 지점 %d개 생성 완료 (edge host-facing %d개)'
            % (
                len(capture_points),
                len(network.topo.edge_switches) * (k // 2),
            )
        )

        # Mininet node 내부 tshark가 결과 파일을 쓸 수 있도록 임시 writable 디렉터리를 사용한다.
        os.makedirs(runtime_capture_dir, exist_ok=True)
        os.chmod(runtime_capture_dir, 0o777)

        if args.capture_mode == 'tshark':
            # PacketCapturer 시작
            capturer = PacketCapturer(
                capture_points=capture_points,
                output_dir=runtime_capture_dir,
                log_dir=os.path.join(runtime_capture_dir, 'logs'),
            )
            capturer.start()
            print('[*] tshark 패킷 캡처 시작 (임시 저장 위치: %s)' % runtime_capture_dir)
        elif args.capture_mode in ('xdp', 'xdp-verify'):
            # XDP는 ingress hook이므로 OVS switch port가 아니라 각 host namespace의 h*-eth0에 붙인다.
            xdp_capture_points = [
                CapturePoint(
                    node=host,
                    interface='%s-eth0' % host.name,
                    output_name=host.name,
                )
                for host in network.hosts
            ]
            capturer = NodeXdpPacketCapturer(
                capture_points=xdp_capture_points,
                log_dir=os.path.join(runtime_capture_dir, 'logs'),
                xdp_mode=args.xdp_mode,
                feature_packet_count=args.feature_packet_count,
                server_port=DST_PORT,
                k=k,
                run_id=run_id,
                redis_url=args.redis_url,
                redis_stream=args.redis_stream,
                redis_stream_maxlen=args.redis_stream_maxlen,
                publish_direction=args.publish_direction,
            )
            if args.capture_mode == 'xdp-verify':
                tshark_capture_points = [
                    CapturePoint(
                        node=host,
                        interface='%s-eth0' % host.name,
                        capture_filter='tcp and dst host %s' % host.IP(),
                        output_name='%s.ingress' % host.name,
                    )
                    for host in network.hosts
                ]
                tshark_capturer = PacketCapturer(
                    capture_points=tshark_capture_points,
                    output_dir=runtime_capture_dir,
                    log_dir=os.path.join(runtime_capture_dir, 'logs'),
                )
                capturer = CombinedPacketCapturer([tshark_capturer, capturer])
            capturer.start()
            if args.capture_mode == 'xdp-verify':
                print('[*] XDP+tshark 검증 캡처 시작 (mode=%s, host 인터페이스 %d개)' % (
                    args.xdp_mode,
                    len(xdp_capture_points),
                ))
            else:
                print('[*] XDP 패킷 캡처 시작 (mode=%s, host 인터페이스 %d개)' % (
                    args.xdp_mode,
                    len(xdp_capture_points),
                ))
            if args.redis_url is not None:
                print('[*] Redis Stream 전송 활성화 (stream=%s, direction=%s)' % (
                    args.redis_stream,
                    args.publish_direction,
                ))
        else:
            print('[*] 패킷 캡처 비활성화 (--capture-mode none)')

        # 트래픽 생성 (pod0,1=src → pod2,3=dst)
        print('[*] 트래픽 생성 시작...')
        flowGenerator(
            network,
            DST_PORT,
            flowsPerPair,
            results_dir=results_dir,
            cdf_file=args.cdf_file,
            load_mbps=args.load_mbps,
            seed_base=args.seed_base,
        )

        time.sleep(1)

        if capturer is not None:
            capturer.stop()
            print('[*] 패킷 캡처 중단')

        # 실험 중 생성된 pcap 또는 XDP 로그를 run별 디렉터리에 복사한다.
        shutil.copytree(runtime_capture_dir, capture_dir, dirs_exist_ok=True)
        make_tree_readable(run_dir)
        print('[*] 패킷 캡처 파일 복사 완료 (%s)' % capture_dir)
        print('[*] run 결과 위치: %s' % run_dir)

        # print('[*] ECMP 검증용 uplink pcap 요약 생성 중...')
        # run_ecmp_verification(
        #     pcap_dir='captured_packet',
        #     results_dir='results',
        # )
        # print('[*] ECMP 검증 결과 저장 완료 (results/ecmp_*)')

        # 결과 분석
        result_script = 'TrafficGenerator/src/script/result.py'
        for i in range(2 * (k // 2) ** 2):  # src 수 = pod0+pod1 호스트 수 = 2*(k/2)^2
            flows_file = os.path.join(results_dir, 'flows_%s.txt' % i)
            if os.path.exists(flows_file):
                print('[*] 결과 분석 중... (%s)' % flows_file)
                os.system('python3 %s %s' % (result_script, flows_file))
            else:
                print('[!] %s 없음 - 결과를 분석할 수 없습니다.' % flows_file)

    finally:
        if capturer is not None:
            try:
                capturer.stop()
            except Exception as exc:
                print('[!] 패킷 캡처 정리 중 오류: %s' % exc)
        if os.path.exists(runtime_capture_dir):
            shutil.copytree(runtime_capture_dir, capture_dir, dirs_exist_ok=True)
            make_tree_readable(run_dir)
        # 정리
        print('[*] 네트워크 종료 중...')
        if network is not None:
            network.stop()
        ryu_proc.terminate()
        os.system('mn -c')
        os.system('pkill -KILL tshark')
