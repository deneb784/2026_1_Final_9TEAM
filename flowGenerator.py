"""Generate flows using the TrafficGenerator tool.
   https://github.com/HKUST-SING/TrafficGenerator """

import os, time
import subprocess

from mininet.cli import CLI

def flowGenerator(
    network,
    port,
    flowsPerPair,
    results_dir="results",
    cdf_file="UNI1_CDF.txt",
    load_mbps=80,
    seed_base=1,
):
    """
    Mininet 네트워크에서 트래픽 플로우를 생성하는 메인 함수.

    Args:
        network: Mininet 네트워크 객체
        port: dst가 수신 대기할 TCP 포트 번호
        flowsPerPair: src-dst 쌍 당 전송할 플로우(요청) 수
    """
    workDirectory = os.getcwd()

    # results 폴더 없으면 생성
    os.makedirs(results_dir, exist_ok=True)

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

        # 요청 크기 분포(CDF) 파일 지정
        configLines.append('req_size_dist TrafficGenerator/conf/%s' % cdf_file)

        # 각 src마다 CDF 파일 (DCTCP) 지정
        with open('TrafficGenerator/conf/src%s_config.txt' % srcIndex, 'w') as configFile:
            for line in configLines:
                configFile.write(line)
                configFile.write('\n')

    # 이전 실행 결과 파일이 있으면 삭제
    for f in [os.path.join(results_dir, 'flows.txt')] + [
        os.path.join(results_dir, 'flows_%s.txt' % i)
        for i in range(len(srcHosts))
    ] + [
        os.path.join(results_dir, 'flows_%s_meta.csv' % i)
        for i in range(len(srcHosts))
    ] + [
        os.path.join(results_dir, 'log_%s.txt' % i)
        for i in range(len(srcHosts))
    ]:
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

    # Mininet Host 객체를 multiprocessing 자식 프로세스로 넘기지 않고,
    # 각 host shell에서 직접 client를 병렬 실행한다.
    processes = []
    for srcIndex, srcHost in enumerate(srcHosts):
        process = startSrc(
            srcHost,
            flowsPerPair,
            srcIndex,
            results_dir=results_dir,
            load_mbps=load_mbps,
            seed=seed_base + srcIndex,
        )
        processes.append((srcIndex, srcHost, process))

    for srcIndex, srcHost, process in processes:
        return_code = process.wait()
        log_file = getattr(process, '_log_file', None)
        if log_file is not None:
            log_file.close()
        if return_code != 0:
            print('[!] %s client 종료 코드: %s' % (srcHost.name, return_code))

    print('src requests done...')


def startDst(dstHosts, port):
    """
    각 dst 호스트에서 TrafficGenerator 서버 데몬을 백그라운드로 실행.
    """
    for dst in dstHosts:
        print('open ' + dst.name + ' port#%s' % port)
        dst.cmd('TrafficGenerator/bin/server -p %s &' % port)


def startSrc(srcHost, flowsPerPair, srcIndex, results_dir="results", load_mbps=80, seed=1):
    """
    특정 src 호스트에서 TrafficGenerator 클라이언트를 실행하여 트래픽을 생성.
    """
    print('start flows from ' + srcHost.name)
    print(srcHost.name + ' flow generate ' + time.strftime('%Y.%m.%d - %H:%M:%S'))
    command = (
        'TrafficGenerator/bin/client -b %s '
        '-c TrafficGenerator/conf/src%s_config.txt '
        '-n %s -l %s -s %s -x %s -v'
        % (
            load_mbps,
            srcIndex,
            flowsPerPair,
            os.path.join(results_dir, 'flows_%s.txt' % srcIndex),
            seed,
            srcIndex,
        )
    )
    log_path = os.path.join(results_dir, 'log_%s.txt' % srcIndex)
    log_file = open(log_path, 'w')
    process = srcHost.popen(
        command,
        shell=True,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    process._log_file = log_file
    return process
