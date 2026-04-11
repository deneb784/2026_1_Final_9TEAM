"""Generate flows using the TrafficGenerator tool.
   https://github.com/HKUST-SING/TrafficGenerator """

import os, time
import multiprocessing

from mininet.cli import CLI

def flowGenerator(network, port, flowsPerPair):
    """
    Mininet 네트워크에서 트래픽 플로우를 생성하는 메인 함수.

    Args:
        network: Mininet 네트워크 객체
        port: dst가 수신 대기할 TCP 포트 번호
        flowsPerPair: src-dst 쌍 당 전송할 플로우(요청) 수
    """
    workDirectory = os.getcwd()

    # results 폴더 없으면 생성
    os.makedirs('results', exist_ok=True)

    allHosts = network.hosts
    srcHosts = []
    dstHosts = []

    # pod0, pod1 = src / pod2, pod3 = dst
    for host in allHosts:
        ip = host.IP()
        if ip.startswith('10.0.') or ip.startswith('10.1.'):
            srcHosts.append(host)
        elif ip.startswith('10.2.') or ip.startswith('10.3.'):
            dstHosts.append(host)

    # 각 src에 대해 TrafficGenerator 설정 파일 생성
    for srcIndex in range(len(srcHosts)):
        configLines = []

        # 모든 dst 호스트를 server로 등록
        for dst in dstHosts:
            configLines.append('server %s %s' % (dst.params['ip'], port))

        # 요청 크기 분포(CDF) 파일 지정 (DCTCP 트래픽 패턴 사용)
        cdf = 'DCTCP_CDF.txt'
        configLines.append('req_size_dist TrafficGenerator/conf/%s' % cdf)

        # 각 src마다 CDF 파일 (DCTCP) 지정
        with open('TrafficGenerator/conf/src%s_config.txt' % srcIndex, 'w') as configFile:
            for line in configLines:
                configFile.write(line)
                configFile.write('\n')

    # 이전 실행 결과 파일이 있으면 삭제
    for f in ['results/flows.txt'] + ['results/flows_%s.txt' % i for i in range(len(srcHosts))]:
        if os.path.exists(f):
            os.remove(f)

    # 모든 dst 호스트에서 수신 대기 데몬 시작
    startDst(dstHosts, port)

    time.sleep(1)
    print('opened all dst hosts...')

    # 라우팅 및 서버 동작 확인용 ping 테스트
    print('[*] ping 테스트 중...')
    result = srcHosts[0].cmd('ping -c 1 -W 2 %s' % dstHosts[0].IP())
    if '1 received' in result:
        print('[*] ping 성공: 라우팅 정상')
    else:
        print('[!] ping 실패: 라우팅 문제 또는 서버 미응답')
        print(result)

    print('src host count:', len(srcHosts))

    # 각 src를 별도 프로세스로 실행하여 병렬 트래픽 생성
    processes = []
    for srcIndex in range(len(srcHosts)):
        p = multiprocessing.Process(target=startSrc, args=(srcHosts, flowsPerPair, srcIndex))
        p.start()
        processes.append(p)

    for process in processes:
        process.join()

    print('src requests done...')


def startDst(dstHosts, port):
    """
    각 dst 호스트에서 TrafficGenerator 서버 데몬을 백그라운드로 실행.
    """
    for dst in dstHosts:
        print('open ' + dst.name + ' port#%s' % port)
        dst.cmd('TrafficGenerator/bin/server -p %s &' % port)


def startSrc(srcHosts, flowsPerPair, srcIndex):
    """
    특정 src 호스트에서 TrafficGenerator 클라이언트를 실행하여 트래픽을 생성.
    """
    print('start flows from ' + srcHosts[srcIndex].name)
    print(srcHosts[srcIndex].name + ' flow generate ' + time.strftime('%Y.%m.%d - %H:%M:%S'))
    srcHosts[srcIndex].cmd(
        'TrafficGenerator/bin/client -b 900 -c TrafficGenerator/conf/src%s_config.txt -n %s -l results/flows_%s.txt -s 1 -v > results/log_%s.txt'
        % (srcIndex, flowsPerPair, srcIndex, srcIndex)
    )


