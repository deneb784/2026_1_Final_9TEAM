"""ECMP verification helpers based on uplink pcap summaries only."""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path
import re


UPLINK_PCAP_RE = re.compile(
    r"^(?P<switch_type>edge|agg)_p\d+_[ae]\d+-eth(?P<port>\d+)\.pcap$"
)


def _run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    # tshark 호출 실패 시에도 후처리 리포트 생성을 계속 진행할 수 있게 check=False로 둔다.
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _count_packets_and_bytes(pcap_path: str | Path) -> tuple[int, int]:
    # frame.len만 읽어 pcap 전체를 빠르게 패킷 수/바이트 수로 축약한다.
    result = _run_command([
        "tshark",
        "-r",
        str(pcap_path),
        "-T",
        "fields",
        "-e",
        "frame.len",
    ])

    packet_count = 0
    byte_count = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        packet_count += 1
        byte_count += int(float(line))

    return packet_count, byte_count


def summarize_uplink_pcaps(
    pcap_dir: str | Path = "captured_packet",
    output_csv: str | Path = "results/ecmp_uplink_summary.csv",
) -> list[dict]:
    # uplink 포트 pcap만 골라 edge/agg 스위치별 분산 비율 계산의 원자료를 만든다.
    pcap_dir = Path(pcap_dir)
    output_csv = Path(output_csv)
    rows: list[dict] = []

    for pcap_path in sorted(pcap_dir.glob("*.pcap")):
        matched = UPLINK_PCAP_RE.match(pcap_path.name)
        if matched is None:
            continue

        port = int(matched.group("port"))
        if port <= 2:
            continue

        # uplink 포트 pcap에서 패킷/바이트 수를 집계해 실제 분산 비율을 계산한다.
        packet_count, byte_count = _count_packets_and_bytes(pcap_path)
        rows.append({
            "switch_name": pcap_path.stem.rsplit("-eth", 1)[0],
            "switch_type": matched.group("switch_type"),
            "port": port,
            "pcap_file": pcap_path.name,
            "packet_count": packet_count,
            "byte_count": byte_count,
        })

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "switch_name",
                "switch_type",
                "port",
                "pcap_file",
                "packet_count",
                "byte_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows


def write_ecmp_report(
    uplink_rows: list[dict],
    output_path: str | Path = "results/ecmp_verification_report.txt",
) -> None:
    # CSV 요약을 사람이 발표 자료용으로 바로 읽을 수 있는 텍스트 리포트로 변환한다.
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    uplink_by_switch: dict[str, list[dict]] = {}
    for row in uplink_rows:
        uplink_by_switch.setdefault(row["switch_name"], []).append(row)

    with output_path.open("w", encoding="utf-8") as f:
        f.write("ECMP Verification Summary\n")
        f.write("=========================\n\n")

        if not uplink_by_switch:
            f.write("No uplink capture files found.\n")
            return

        for switch_name in sorted(uplink_by_switch):
            rows = sorted(uplink_by_switch[switch_name], key=lambda item: item["port"])
            total_packets = sum(row["packet_count"] for row in rows)
            total_bytes = sum(row["byte_count"] for row in rows)
            f.write("%s\n" % switch_name)
            for row in rows:
                # 스위치별 전체 uplink 트래픽 대비 각 포트 점유율을 함께 기록한다.
                packet_share = 0.0 if total_packets == 0 else row["packet_count"] / total_packets * 100.0
                byte_share = 0.0 if total_bytes == 0 else row["byte_count"] / total_bytes * 100.0
                f.write(
                    "  port %s: packets=%s (%.2f%%), bytes=%s (%.2f%%)\n"
                    % (row["port"], row["packet_count"], packet_share, row["byte_count"], byte_share)
                )
            f.write("\n")


def run_ecmp_verification(
    pcap_dir: str | Path = "captured_packet",
    results_dir: str | Path = "results",
) -> None:
    # 실험 종료 후 호출되는 ECMP 검증 엔트리 포인트다.
    results_dir = Path(results_dir)
    uplink_rows = summarize_uplink_pcaps(
        pcap_dir=pcap_dir,
        output_csv=results_dir / "ecmp_uplink_summary.csv",
    )
    write_ecmp_report(
        uplink_rows=uplink_rows,
        output_path=results_dir / "ecmp_verification_report.txt",
    )
