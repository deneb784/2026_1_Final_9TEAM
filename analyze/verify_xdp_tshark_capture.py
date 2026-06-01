#!/usr/bin/env python3
"""tshark로 센 ingress pcap TCP 패킷 수와 XDP 로그 카운터를 대조하는 검증 스크립트.

실험 실행 결과 디렉터리(run 디렉터리) 또는 그 안의 captured_packet 디렉터리를 입력으로 받아서
호스트별 `h*.ingress.pcap` 파일과 `logs/h*.xdp.stdout.txt` 파일을 짝지어 비교한다.
pcap은 실제 캡처된 패킷 기준, XDP 로그는 커널/eBPF 프로그램이 집계한 카운터 기준이므로
둘이 맞는지 확인하면 XDP capture 경로에서 패킷 누락이나 분류 오류가 있었는지 빠르게 볼 수 있다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


# XDP user-space 로거가 마지막에 찍는 [summary] 라인을 파싱한다.
# 여기서는 전체 이벤트 수(events), 메타데이터 처리 결과, key_errors, 방향 분류 결과를 뽑는다.
# 비교에는 events/key_errors/unknown만 직접 쓰지만, 로그 형식이 깨졌을 때 바로 잡히도록
# 주변 필드까지 포함한 정규식으로 기대하는 한 줄 구조를 엄격하게 확인한다.
SUMMARY_RE = re.compile(
    r"\[summary\].*?events=(?P<events>\d+).*?metadata=(?P<metadata>\d+)"
    r".*?matched=(?P<matched>\d+).*?ready=(?P<ready>\d+)"
    r".*?key_errors=(?P<key_errors>\d+).*?"
    r"directions\(src_to_dst=(?P<src_to_dst>\d+),dst_to_src=(?P<dst_to_src>\d+),unknown=(?P<unknown>\d+)\)"
)

# XDP/eBPF 쪽 커널 카운터가 찍히는 [kernel] 라인을 파싱한다.
# hook부터 tcp_ack_only까지 단계별 카운터가 들어 있어서 어느 처리 단계에서 누락이 생겼는지
# 나중에 추적할 수 있다. 이 스크립트에서는 tshark 결과와 직접 맞춰볼 수 있는
# submit/tcp_payload/tcp_ack_only 값을 사용한다.
KERNEL_RE = re.compile(
    r"\[kernel\].*?hook=(?P<hook>\d+).*?eth_ip=(?P<eth_ip>\d+)"
    r".*?ip_tcp=(?P<ip_tcp>\d+).*?tcp_header=(?P<tcp_header>\d+)"
    r".*?tcp_payload=(?P<tcp_payload>\d+).*?submit=(?P<submit>\d+)"
    r".*?tcp_ack_only=(?P<tcp_ack_only>\d+)"
)


@dataclass(frozen=True)
class TsharkCounts:
    """pcap을 tshark로 읽어서 계산한 TCP 패킷 분류 결과."""

    # total: pcap 안에서 tcp 필터에 걸린 전체 프레임 수
    # payload: tcp.len > 0인 데이터 패킷 수
    # ack_only: tcp.len == 0인 순수 ACK성 패킷 수
    total: int = 0
    payload: int = 0
    ack_only: int = 0


@dataclass(frozen=True)
class XdpCounts:
    """XDP stdout 로그에서 읽어 온 커널/user-space 카운터 중 비교에 필요한 값."""

    # events: perf/ring buffer를 통해 user-space까지 올라온 이벤트 수
    # submit: 커널 쪽에서 이벤트 제출까지 성공한 수
    # tcp_payload/tcp_ack_only: XDP 프로그램이 TCP payload 유무로 분류한 수
    # key_errors: flow key 생성/조회 과정에서 생긴 오류 수. 정상이라면 0이어야 한다.
    # unknown: 방향(src_to_dst/dst_to_src)을 결정하지 못한 수. 정상이라면 0이어야 한다.
    events: int
    submit: int
    tcp_payload: int
    tcp_ack_only: int
    key_errors: int
    unknown: int


def _to_int_dict(match: re.Match[str]) -> dict[str, int]:
    # 정규식 named group은 문자열로 나오므로, 이후 비교가 단순해지도록 한 번에 int로 변환한다.
    return {key: int(value) for key, value in match.groupdict().items()}


def parse_xdp_log(path: Path) -> XdpCounts:
    """XDP stdout 로그 하나에서 summary/kernel 카운터를 읽는다."""

    # 로그가 일부 깨졌거나 비UTF-8 바이트가 섞여도 검증 자체는 계속할 수 있도록
    # errors="replace"를 사용한다. 필요한 라인이 없으면 아래에서 명확히 실패시킨다.
    text = path.read_text(encoding="utf-8", errors="replace")
    summary_match = SUMMARY_RE.search(text)
    kernel_match = KERNEL_RE.search(text)
    if summary_match is None:
        raise ValueError(f"{path}: [summary] line not found")
    if kernel_match is None:
        raise ValueError(f"{path}: [kernel] line not found")

    # summary는 user-space 집계/방향 분류 결과, kernel은 eBPF 내부 처리 단계별 카운터다.
    # 두 줄의 출처가 달라서 필요한 값을 각각 꺼내 하나의 XdpCounts로 합친다.
    summary = _to_int_dict(summary_match)
    kernel = _to_int_dict(kernel_match)
    return XdpCounts(
        events=summary["events"],
        submit=kernel["submit"],
        tcp_payload=kernel["tcp_payload"],
        tcp_ack_only=kernel["tcp_ack_only"],
        key_errors=summary["key_errors"],
        unknown=summary["unknown"],
    )


def count_tshark_tcp_lengths(path: Path) -> TsharkCounts:
    """tshark로 pcap의 tcp.len 필드만 뽑아 payload/ACK-only 패킷 수를 센다."""

    # -n: 이름 해석 비활성화로 속도와 재현성 확보
    # -r: 입력 pcap 파일
    # -Y tcp: TCP 패킷만 display filter로 선택
    # -T fields -e tcp.len: 각 TCP 패킷의 payload 길이만 한 줄씩 출력
    cmd = [
        "tshark",
        "-n",
        "-r",
        str(path),
        "-Y",
        "tcp",
        "-T",
        "fields",
        "-e",
        "tcp.len",
    ]
    process = subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    total = 0
    payload = 0
    ack_only = 0
    for line in process.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        # 일부 패킷은 재조립/중복 필드 때문에 "0,0"처럼 여러 값이 찍힐 수 있다.
        # 여기서는 패킷 1개당 대표 tcp.len 하나만 필요하므로 첫 값만 사용한다.
        if "," in value:
            value = value.split(",", 1)[0]
        # tshark 필드 출력이 정수 문자열이 보통이지만, 환경에 따라 "0.0" 형태가 섞일 수 있어
        # float을 거쳐 int로 변환한다.
        tcp_len = int(float(value))
        total += 1
        if tcp_len == 0:
            ack_only += 1
        else:
            payload += 1
    return TsharkCounts(total=total, payload=payload, ack_only=ack_only)


def compare_host(host: str, tshark: TsharkCounts, xdp: XdpCounts) -> list[str]:
    """호스트 하나의 tshark 기준 카운터와 XDP 기준 카운터를 비교한다."""

    mismatches: list[str] = []
    # tshark_total은 pcap에서 보이는 TCP 패킷 수이고, XDP events/submit은 XDP 경로에서
    # 같은 패킷이 user-space 이벤트로 관측되었는지 보는 값이다.
    # payload/ack_only는 payload 길이 기준 분류가 양쪽에서 동일한지 확인한다.
    # key_errors/unknown은 pcap에는 대응 필드가 없으므로 정상 기대값인 0과 비교한다.
    checks = [
        ("total", tshark.total, xdp.events),
        ("kernel_submit", tshark.total, xdp.submit),
        ("payload", tshark.payload, xdp.tcp_payload),
        ("ack_only", tshark.ack_only, xdp.tcp_ack_only),
        ("key_errors", 0, xdp.key_errors),
        ("unknown", 0, xdp.unknown),
    ]
    for label, expected, actual in checks:
        if expected != actual:
            mismatches.append(f"{host}: {label} expected={expected} actual={actual}")
    return mismatches


def find_capture_dir(path: Path) -> Path:
    """run 디렉터리와 captured_packet 디렉터리 입력을 모두 허용하기 위한 보정."""

    # 사용자가 run 루트를 넘기면 그 안의 captured_packet을 사용하고,
    # 이미 captured_packet을 넘겼다면 그대로 사용한다.
    if (path / "captured_packet").is_dir():
        return path / "captured_packet"
    return path


def main() -> int:
    # 실행 예:
    #   python analyze/verify_xdp_tshark_capture.py runs/xdp-verify-...
    #   python analyze/verify_xdp_tshark_capture.py runs/xdp-verify-.../captured_packet
    parser = argparse.ArgumentParser(
        description="xdp-verify run에서 tshark ingress pcap과 XDP 로그 카운터를 비교한다.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="run 디렉터리 또는 captured_packet 디렉터리",
    )
    args = parser.parse_args()

    # captured_packet 아래에는 h*.ingress.pcap 파일이 있고,
    # captured_packet/logs 아래에는 호스트별 XDP stdout 로그가 있어야 한다.
    capture_dir = find_capture_dir(args.path)
    log_dir = capture_dir / "logs"
    if not capture_dir.is_dir():
        raise SystemExit(f"capture directory not found: {capture_dir}")
    if not log_dir.is_dir():
        raise SystemExit(f"log directory not found: {log_dir}")

    pcap_files = sorted(capture_dir.glob("h*.ingress.pcap"))
    if not pcap_files:
        raise SystemExit(f"no h*.ingress.pcap files found in {capture_dir}")

    all_mismatches: list[str] = []
    # 마지막 TOTAL 행을 출력하기 위해 호스트별 결과를 누적한다.
    totals = {
        "tshark_total": 0,
        "xdp_events": 0,
        "tshark_payload": 0,
        "xdp_payload": 0,
        "tshark_ack_only": 0,
        "xdp_ack_only": 0,
    }

    print("host,tshark_total,xdp_events,tshark_payload,xdp_payload,tshark_ack_only,xdp_ack_only")
    for pcap_file in pcap_files:
        # 파일명 규칙: h1.ingress.pcap -> host == h1
        # 로그명 규칙: logs/h1.xdp.stdout.txt
        host = pcap_file.name.removesuffix(".ingress.pcap")
        xdp_log = log_dir / f"{host}.xdp.stdout.txt"
        if not xdp_log.exists():
            all_mismatches.append(f"{host}: missing XDP log {xdp_log}")
            continue

        tshark_counts = count_tshark_tcp_lengths(pcap_file)
        xdp_counts = parse_xdp_log(xdp_log)
        all_mismatches.extend(compare_host(host, tshark_counts, xdp_counts))

        # CSV 행 출력은 호스트별 상세, totals는 전체 합계 행에 사용한다.
        totals["tshark_total"] += tshark_counts.total
        totals["xdp_events"] += xdp_counts.events
        totals["tshark_payload"] += tshark_counts.payload
        totals["xdp_payload"] += xdp_counts.tcp_payload
        totals["tshark_ack_only"] += tshark_counts.ack_only
        totals["xdp_ack_only"] += xdp_counts.tcp_ack_only

        print(
            f"{host},{tshark_counts.total},{xdp_counts.events},"
            f"{tshark_counts.payload},{xdp_counts.tcp_payload},"
            f"{tshark_counts.ack_only},{xdp_counts.tcp_ack_only}"
        )

    print(
        "TOTAL,{tshark_total},{xdp_events},{tshark_payload},{xdp_payload},"
        "{tshark_ack_only},{xdp_ack_only}".format(**totals)
    )

    # 하나라도 불일치가 있으면 CI/쉘에서 실패로 감지할 수 있도록 exit code 1을 반환한다.
    if all_mismatches:
        print("\n[FAIL] mismatches found:")
        for mismatch in all_mismatches:
            print(f"- {mismatch}")
        return 1

    print("\n[OK] tshark ingress pcap counts match XDP counters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
