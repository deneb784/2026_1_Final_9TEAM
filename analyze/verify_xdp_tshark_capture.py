#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


SUMMARY_RE = re.compile(
    r"\[summary\].*?events=(?P<events>\d+).*?metadata=(?P<metadata>\d+)"
    r".*?matched=(?P<matched>\d+).*?ready=(?P<ready>\d+)"
    r".*?key_errors=(?P<key_errors>\d+).*?"
    r"directions\(src_to_dst=(?P<src_to_dst>\d+),dst_to_src=(?P<dst_to_src>\d+),unknown=(?P<unknown>\d+)\)"
)
KERNEL_RE = re.compile(
    r"\[kernel\].*?hook=(?P<hook>\d+).*?eth_ip=(?P<eth_ip>\d+)"
    r".*?ip_tcp=(?P<ip_tcp>\d+).*?tcp_header=(?P<tcp_header>\d+)"
    r".*?tcp_payload=(?P<tcp_payload>\d+).*?submit=(?P<submit>\d+)"
    r".*?tcp_ack_only=(?P<tcp_ack_only>\d+)"
)


@dataclass(frozen=True)
class TsharkCounts:
    total: int = 0
    payload: int = 0
    ack_only: int = 0


@dataclass(frozen=True)
class XdpCounts:
    events: int
    submit: int
    tcp_payload: int
    tcp_ack_only: int
    key_errors: int
    unknown: int


def _to_int_dict(match: re.Match[str]) -> dict[str, int]:
    return {key: int(value) for key, value in match.groupdict().items()}


def parse_xdp_log(path: Path) -> XdpCounts:
    text = path.read_text(encoding="utf-8", errors="replace")
    summary_match = SUMMARY_RE.search(text)
    kernel_match = KERNEL_RE.search(text)
    if summary_match is None:
        raise ValueError(f"{path}: [summary] line not found")
    if kernel_match is None:
        raise ValueError(f"{path}: [kernel] line not found")

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
        if "," in value:
            value = value.split(",", 1)[0]
        tcp_len = int(float(value))
        total += 1
        if tcp_len == 0:
            ack_only += 1
        else:
            payload += 1
    return TsharkCounts(total=total, payload=payload, ack_only=ack_only)


def compare_host(host: str, tshark: TsharkCounts, xdp: XdpCounts) -> list[str]:
    mismatches: list[str] = []
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
    if (path / "captured_packet").is_dir():
        return path / "captured_packet"
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="xdp-verify run에서 tshark ingress pcap과 XDP 로그 카운터를 비교한다.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="run 디렉터리 또는 captured_packet 디렉터리",
    )
    args = parser.parse_args()

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
        host = pcap_file.name.removesuffix(".ingress.pcap")
        xdp_log = log_dir / f"{host}.xdp.stdout.txt"
        if not xdp_log.exists():
            all_mismatches.append(f"{host}: missing XDP log {xdp_log}")
            continue

        tshark_counts = count_tshark_tcp_lengths(pcap_file)
        xdp_counts = parse_xdp_log(xdp_log)
        all_mismatches.extend(compare_host(host, tshark_counts, xdp_counts))

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

    if all_mismatches:
        print("\n[FAIL] mismatches found:")
        for mismatch in all_mismatches:
            print(f"- {mismatch}")
        return 1

    print("\n[OK] tshark ingress pcap counts match XDP counters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
