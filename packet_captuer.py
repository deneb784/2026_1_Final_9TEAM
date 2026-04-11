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


class PacketCapturer:
    """여러 캡처 지점에서 tshark를 실행해 각각 pcap 파일로 저장한다."""

    def __init__(
        self,
        capture_points: list[CapturePoint],
        output_dir: str = "captured_packet",
    ):
        """캡처 지점 목록, 저장 디렉터리를 초기화한다."""
        self.capture_points = capture_points
        self.output_dir = output_dir
        self.processes: list[subprocess.Popen] = []

    def start(self) -> None:
        """모든 캡처 지점에서 tshark subprocess를 시작하고 pcap 파일로 기록한다."""
        os.makedirs(self.output_dir, exist_ok=True)
        for cp in self.capture_points:
            output_file = os.path.join(self.output_dir, "%s.pcap" % cp.interface)

            filters = ["tcp"]
            if cp.src_ips:
                ip_filter = " or ".join("src host %s" % ip for ip in cp.src_ips)
                filters.append("(%s)" % ip_filter)

            cmd = ["tshark", "-i", cp.interface, "-w", output_file]
            if filters:
                cmd += ["-f", " and ".join(filters)]

            process = cp.node.popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
