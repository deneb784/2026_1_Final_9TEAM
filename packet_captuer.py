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
        server_port: int | None = None,
    ):
        """캡처 지점 목록, 저장 디렉터리, 필터링할 포트를 초기화한다."""
        self.capture_points = capture_points
        self.output_dir = output_dir
        self.server_port = server_port
        self.processes: list[subprocess.Popen] = []

    def start(self) -> None:
        """모든 캡처 지점에서 tshark subprocess를 시작하고 pcap 파일로 기록한다."""
        os.makedirs(self.output_dir, exist_ok=True)
        for cp in self.capture_points:
            output_file = os.path.join(self.output_dir, "%s.pcap" % cp.interface)

            filters = []
            if self.server_port is not None:
                filters.append("tcp and port %d" % self.server_port)
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
        """모든 tshark subprocess를 안전하게 종료하고 src IP 기준으로 후처리 필터링한다."""
        for process in self.processes:
            if process.poll() is not None:
                continue
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()

        for cp in self.capture_points:
            if not cp.src_ips:
                continue
            pcap_file = os.path.join(self.output_dir, "%s.pcap" % cp.interface)
            if not os.path.exists(pcap_file):
                continue
            ip_filter = " or ".join("ip.src == %s" % ip for ip in cp.src_ips)
            tmp_file = pcap_file + ".tmp"
            subprocess.run(
                ["tshark", "-r", pcap_file, "-Y", ip_filter, "-w", tmp_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.replace(tmp_file, pcap_file)
