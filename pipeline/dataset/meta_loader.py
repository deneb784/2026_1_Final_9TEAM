import csv
from collections import defaultdict
from glob import glob
from pathlib import Path

from pipeline.models import RequestMeta


def parse_request_meta_row(row: dict) -> RequestMeta:
    return RequestMeta(
        src_index=int(row["src_index"]),
        flow_id=int(row["flow_id"]),
        server_id=int(row["server_id"]),
        connection_id=int(row["connection_id"]),
        src_ip=row["src_ip"],
        src_port=int(row["src_port"]),
        dst_ip=row["dst_ip"],
        dst_port=int(row["dst_port"]),
        size_bytes=int(row["size_bytes"]),
        dscp=int(row["dscp"]),
        rate_mbps=int(row["rate_mbps"]),
        start_time_us=int(row["start_time_us"]),
        stop_time_us=int(row["stop_time_us"]),
        fct_us=int(row["fct_us"]),
        src_to_dst_flow_id=row.get("src_to_dst_flow_id", ""),
        dst_to_src_flow_id=row.get("dst_to_src_flow_id", ""),
        src_to_dst_tos=int(row.get("src_to_dst_tos", 0)),
        dst_to_src_tos=int(row.get("dst_to_src_tos", 0)),
    )


def load_request_meta_file(path: str | Path) -> list[RequestMeta]:
    metas: list[RequestMeta] = []

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            metas.append(parse_request_meta_row(row))

    return metas


def load_all_request_meta(results_dir: str | Path) -> list[RequestMeta]:
    pattern = str(Path(results_dir) / "flows_*_meta.csv")
    meta_files = sorted(glob(pattern))

    all_metas: list[RequestMeta] = []
    for meta_file in meta_files:
        all_metas.extend(load_request_meta_file(meta_file))

    return all_metas


def build_meta_index(
    all_metas: list[RequestMeta],
) -> dict[tuple[str, int, str, int], list[RequestMeta]]:
    meta_index = defaultdict(list)

    for meta in all_metas:
        key = (meta.src_ip, meta.src_port, meta.dst_ip, meta.dst_port)
        meta_index[key].append(meta)

    for key in meta_index:
        meta_index[key].sort(key=lambda m: m.start_time_us)

    return dict(meta_index)
