"""모든 edge switch의 패킷을 pcap 파일로 저장하는 캡처 모듈"""

import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CapturePoint:
    """어느 node/interface에서 packet을 잡을지 나타내는 캡처 지점."""

    node: object
    interface: str
    src_ips: tuple[str, ...] = ()
    # 검증용 uplink 캡처처럼 기본 host-facing 필터와 다른 경우 별도 필터를 사용한다.
    capture_filter: str | None = None
    # 결과 파일명을 인터페이스명과 분리해 후처리 입력을 고정하기 위해 사용한다.
    output_name: str | None = None


class PacketCapturer:
    """여러 캡처 지점에서 tshark를 실행해 각각 pcap 파일로 저장한다."""

    def __init__(
        self,
        capture_points: list[CapturePoint],
        output_dir: str = "captured_packet",
        log_dir: str | None = None,
    ):
        """캡처 지점 목록, 저장 디렉터리를 초기화한다."""
        self.capture_points = capture_points
        self.output_dir = output_dir
        self.log_dir = log_dir
        self.processes: list[subprocess.Popen] = []

    def start(self) -> None:
        """모든 캡처 지점에서 tshark subprocess를 시작하고 pcap 파일로 기록한다."""
        os.makedirs(self.output_dir, exist_ok=True)
        if self.log_dir is not None:
            os.makedirs(self.log_dir, exist_ok=True)
        for cp in self.capture_points:
            output_name = cp.output_name or cp.interface
            output_file = os.path.join(self.output_dir, "%s.pcap" % output_name)

            if cp.capture_filter is not None:
                capture_filter = cp.capture_filter
            else:
                filters = ["tcp"]
                if cp.src_ips:
                    ip_filter = " or ".join("src host %s" % ip for ip in cp.src_ips)
                    filters.append("(%s)" % ip_filter)
                capture_filter = " and ".join(filters)

            cmd = ["tshark", "-i", cp.interface, "-w", output_file]
            if capture_filter:
                cmd += ["-f", capture_filter]

            stderr_target = subprocess.DEVNULL
            if self.log_dir is not None:
                # tshark 실행 실패 원인을 남겨 uplink 캡처 문제를 바로 확인할 수 있게 한다.
                stderr_target = open(
                    os.path.join(self.log_dir, "%s.stderr.txt" % output_name),
                    "w",
                    encoding="utf-8",
                )

            process = cp.node.popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
            )
            process._stderr_target = stderr_target
            self.processes.append(process)

    def stop(self) -> None:
        """모든 tshark subprocess를 안전하게 종료한다."""
        for process in self.processes:
            if process.poll() is not None:
                continue
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
            stderr_target = getattr(process, "_stderr_target", None)
            if stderr_target not in (None, subprocess.DEVNULL):
                stderr_target.close()
