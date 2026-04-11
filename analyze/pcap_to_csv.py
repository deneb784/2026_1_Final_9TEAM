"""captured_packet/ 디렉터리의 pcap 파일들을 하나의 CSV로 변환하는 모듈"""

import os
import csv
import subprocess

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PCAP_DIR = os.path.join(ROOT_DIR, "captured_packet")
OUTPUT_CSV = os.path.join(ROOT_DIR, "csv_results", "packets.csv")

FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "ip.src",
    "ip.dst",
    "ip.len",
    "ip.ttl",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "tcp.len",
    "tcp.seq",
    "tcp.ack",
    "tcp.flags",
    "tcp.window_size",
    "tcp.time_relative",
    "tcp.time_delta",
    "tcp.analysis.retransmission",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.fast_retransmission",
]


def pcap_to_rows(pcap_file: str) -> list[list[str]]:
    """tshark로 pcap 파일을 읽어 행 목록으로 반환한다."""
    cmd = ["tshark", "-r", pcap_file, "-T", "fields"] + [
        arg for field in FIELDS for arg in ("-e", field)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    rows = []
    for line in result.stdout.splitlines():
        values = line.split("\t")
        if len(values) == len(FIELDS):
            rows.append(values)
    return rows


def convert(pcap_dir: str = PCAP_DIR, output_csv: str = OUTPUT_CSV) -> None:
    """pcap_dir의 모든 pcap 파일을 읽어 output_csv로 저장한다."""
    pcap_files = sorted(
        os.path.join(pcap_dir, f)
        for f in os.listdir(pcap_dir)
        if f.endswith(".pcap")
    )

    if not pcap_files:
        print("[!] pcap 파일이 없습니다: %s" % pcap_dir)
        return

    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    with open(output_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_file"] + FIELDS)

        for pcap_file in pcap_files:
            source = os.path.basename(pcap_file)
            rows = pcap_to_rows(pcap_file)
            for row in rows:
                writer.writerow([source] + row)
            print("[*] %s → %d 패킷" % (source, len(rows)))

    print("[*] 저장 완료: %s" % output_csv)


if __name__ == "__main__":
    convert()
