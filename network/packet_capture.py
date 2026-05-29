"""모든 edge switch의 패킷을 pcap 파일로 저장하는 캡처 모듈"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


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


class CombinedPacketCapturer:
    """여러 capturer를 하나처럼 시작/종료하기 위한 얇은 wrapper."""

    def __init__(self, capturers: list[object]):
        self.capturers = capturers
        self._started: list[object] = []

    def start(self) -> None:
        try:
            for capturer in self.capturers:
                capturer.start()
                self._started.append(capturer)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for capturer in reversed(self._started):
            capturer.stop()
        self._started.clear()


class XdpPacketCapturer:
    """여러 인터페이스에 XDP 캡처 프로그램을 붙여 온라인 FlowCache까지 전달한다."""

    def __init__(
        self,
        interfaces: list[str],
        log_dir: str | None = None,
        xdp_mode: str = "skb",
        feature_packet_count: int = 10,
        server_port: int = 5001,
        k: int = 4,
        run_id: str | None = None,
        redis_url: str | None = None,
        redis_stream: str = "flow_features",
        redis_stream_maxlen: int | None = None,
        redis_response_channel: str = "flow_results",
        classification_log: str | None = None,
        publish_direction: str = "dst_to_src",
        project_root: str | None = None,
        startup_wait_sec: float = 3.0,
    ):
        self.interfaces = interfaces
        self.log_dir = log_dir
        self.xdp_mode = xdp_mode
        self.feature_packet_count = feature_packet_count
        self.server_port = server_port
        self.k = k
        self.run_id = run_id
        self.redis_url = redis_url
        self.redis_stream = redis_stream
        self.redis_stream_maxlen = redis_stream_maxlen
        self.redis_response_channel = redis_response_channel
        self.classification_log = classification_log
        self.publish_direction = publish_direction
        self.project_root = project_root or PROJECT_ROOT
        self.startup_wait_sec = startup_wait_sec
        self.process: subprocess.Popen | None = None
        self._stdout_target = None
        self._stderr_target = None

    def start(self) -> None:
        """XDP 캡처 subprocess를 시작한다."""
        if not self.interfaces:
            return

        if self.log_dir is not None:
            os.makedirs(self.log_dir, exist_ok=True)
            self._stdout_target = open(
                os.path.join(self.log_dir, "xdp_capture.stdout.txt"),
                "w",
                encoding="utf-8",
            )
            self._stderr_target = open(
                os.path.join(self.log_dir, "xdp_capture.stderr.txt"),
                "w",
                encoding="utf-8",
            )
        else:
            self._stdout_target = subprocess.DEVNULL
            self._stderr_target = subprocess.DEVNULL

        script = os.path.join(self.project_root, "xdp", "tg_xdp_capture.py")
        cmd = [
            sys.executable,
            script,
            "-i",
            *self.interfaces,
            "--xdp-mode",
            self.xdp_mode,
            "--feature-packet-count",
            str(self.feature_packet_count),
            "--server-port",
            str(self.server_port),
            "--k",
            str(self.k),
            "--publish-direction",
            self.publish_direction,
            "--print-ready",
        ]
        if self.run_id is not None:
            cmd += ["--run-id", self.run_id]
        if self.redis_url is not None:
            cmd += ["--redis-url", self.redis_url, "--redis-stream", self.redis_stream]
            cmd += ["--redis-response-channel", self.redis_response_channel]
            if self.classification_log is not None:
                cmd += ["--classification-log", self.classification_log]
            if self.redis_stream_maxlen is not None:
                cmd += ["--redis-stream-maxlen", str(self.redis_stream_maxlen)]
        self.process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=self._stdout_target,
            stderr=self._stderr_target,
        )
        time.sleep(self.startup_wait_sec)
        if self.process.poll() is not None:
            raise RuntimeError("XDP 캡처 프로세스가 시작 직후 종료되었습니다. xdp_capture.stderr.txt를 확인하세요.")

    def stop(self) -> None:
        """XDP 캡처 subprocess를 종료해 attach된 XDP 프로그램을 detach한다."""
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        for target in (self._stdout_target, self._stderr_target):
            if target not in (None, subprocess.DEVNULL):
                target.close()


class NodeXdpPacketCapturer:
    """각 Mininet node namespace 안에서 host-facing 인터페이스에 XDP를 붙인다."""

    def __init__(
        self,
        capture_points: list[CapturePoint],
        log_dir: str | None = None,
        xdp_mode: str = "skb",
        feature_packet_count: int = 10,
        server_port: int = 5001,
        k: int = 4,
        run_id: str | None = None,
        redis_url: str | None = None,
        redis_stream: str = "flow_features",
        redis_stream_maxlen: int | None = None,
        redis_response_channel: str = "flow_results",
        classification_log: str | None = None,
        publish_direction: str = "dst_to_src",
        project_root: str | None = None,
        startup_wait_sec: float = 3.0,
    ):
        self.capture_points = capture_points
        self.log_dir = log_dir
        self.xdp_mode = xdp_mode
        self.feature_packet_count = feature_packet_count
        self.server_port = server_port
        self.k = k
        self.run_id = run_id
        self.redis_url = redis_url
        self.redis_stream = redis_stream
        self.redis_stream_maxlen = redis_stream_maxlen
        self.redis_response_channel = redis_response_channel
        self.classification_log = classification_log
        self.publish_direction = publish_direction
        self.project_root = project_root or PROJECT_ROOT
        self.startup_wait_sec = startup_wait_sec
        self.processes: list[subprocess.Popen] = []

    def start(self) -> None:
        """각 capture point의 node namespace에서 XDP 캡처 subprocess를 시작한다."""
        if self.log_dir is not None:
            os.makedirs(self.log_dir, exist_ok=True)

        script = os.path.join(self.project_root, "xdp", "tg_xdp_capture.py")
        for cp in self.capture_points:
            output_name = cp.output_name or cp.interface
            stdout_target = subprocess.DEVNULL
            stderr_target = subprocess.DEVNULL
            if self.log_dir is not None:
                stdout_target = open(
                    os.path.join(self.log_dir, "%s.xdp.stdout.txt" % output_name),
                    "w",
                    encoding="utf-8",
                )
                stderr_target = open(
                    os.path.join(self.log_dir, "%s.xdp.stderr.txt" % output_name),
                    "w",
                    encoding="utf-8",
                )

            cmd = [
                sys.executable,
                script,
                "-i",
                cp.interface,
                "--xdp-mode",
                self.xdp_mode,
                "--feature-packet-count",
                str(self.feature_packet_count),
                "--server-port",
                str(self.server_port),
                "--k",
                str(self.k),
                "--publish-direction",
                self.publish_direction,
                "--print-ready",
            ]
            if self.run_id is not None:
                cmd += ["--run-id", self.run_id]
            if self.redis_url is not None:
                cmd += ["--redis-url", self.redis_url, "--redis-stream", self.redis_stream]
                cmd += ["--redis-response-channel", self.redis_response_channel]
                if self.classification_log is not None:
                    cmd += ["--classification-log", self.classification_log]
                if self.redis_stream_maxlen is not None:
                    cmd += ["--redis-stream-maxlen", str(self.redis_stream_maxlen)]
            process = cp.node.popen(
                cmd,
                cwd=self.project_root,
                stdout=stdout_target,
                stderr=stderr_target,
            )
            process._stdout_target = stdout_target
            process._stderr_target = stderr_target
            process._output_name = output_name
            self.processes.append(process)

        time.sleep(self.startup_wait_sec)
        failed = [
            getattr(process, "_output_name", "unknown")
            for process in self.processes
            if process.poll() is not None
        ]
        if failed:
            raise RuntimeError(
                "XDP 캡처 프로세스가 시작 직후 종료되었습니다: %s. "
                "해당 *.xdp.stderr.txt를 확인하세요." % ", ".join(failed)
            )

    def stop(self) -> None:
        """모든 node namespace XDP 캡처 subprocess를 종료한다."""
        for process in self.processes:
            if process.poll() is not None:
                continue
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        for process in self.processes:
            for target_name in ("_stdout_target", "_stderr_target"):
                target = getattr(process, target_name, None)
                if target not in (None, subprocess.DEVNULL):
                    target.close()
