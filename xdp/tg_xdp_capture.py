#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes as ct
import socket
import struct
import sys
import time
from pathlib import Path

from bcc import BPF

sys.path.append(str(Path(__file__).resolve().parents[1]))

from pipeline.realtime.online_tg_flow_cache import (
    parse_tg_metadata,
    TrafficGeneratorOnlineFlowCache,
    XdpPacketEvent,
    build_default_src_index_by_ip,
)
from pipeline.realtime.online_request import build_online_flow_request
from pipeline.redis.transport import RedisStreamProducer


# MAX_PAYLOAD_PREFIX는 캡처해 올 payload prefix(앞부분) 최대 바이트 수입니다.
# XDP/BPF 쪽에서 패킷의 payload 앞부분만 읽어와 user-space로 전달합니다.
# 이렇게 하면 커널에서 무거운 작업을 피하고, 사용자 공간에서 flow 식별과
# 상세 처리를 수행할 수 있습니다.
MAX_PAYLOAD_PREFIX = 32


# BPF 프로그램(문자열)은 BCC에 의해 런타임에 컴파일되어 네트워크 인터페이스에
# XDP hook으로 붙습니다. 이 프로그램은 매우 제한된 환경(예: 루프 금지, 제한된
# 스택 등)에서 동작하므로 가능한 최소한의 검사와 바이트 추출만 수행합니다.
#
# - Ethernet/IP/TCP 헤더 유효성 검사
# - TCP payload 앞부분을 최대 MAX_PAYLOAD_PREFIX 바이트까지 읽음
# - 읽은 정보를 perf 이벤트로 user-space에 전달
BPF_PROGRAM = r"""
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <uapi/linux/in.h>

#define MAX_PAYLOAD_PREFIX 32

#define STAT_HOOK 0
#define STAT_ETH_IP 1
#define STAT_IP_TCP 2
#define STAT_TCP_HEADER 3
#define STAT_TCP_PAYLOAD 4
#define STAT_SUBMIT 5
#define STAT_TCP_ACK_ONLY 6

BPF_ARRAY(kernel_stats, __u64, 8);

/*
 * kernel_stats 맵 설명:
 * - BPF_ARRAY 'kernel_stats'는 런타임에 각 단계별 카운터를 유지합니다.
 * - 사용자 공간은 이 맵을 읽어 XDP에서 처리된 패킷 통계를 확인합니다.
 */

static __always_inline void count_stat(__u32 index)
{
    __u64 *value = kernel_stats.lookup(&index);
    if (value)
        __sync_fetch_and_add(value, 1);
}

/*
 * count_stat:
 * - 각 처리 지점에서 호출되어 kernel_stats 맵의 해당 인덱스 값을 원자적으로 증가시킵니다.
 * - __sync_fetch_and_add를 사용하여 concurrency 안전하게 카운트를 증가시킵니다.
 */

struct packet_event_t {
    __u64 ts_ns;
    __u32 ifindex;
    __u32 frame_len;
    __u32 saddr;
    __u32 daddr;
    __u32 seq;
    __u32 ack_seq;
    __u16 sport;
    __u16 dport;
    __u16 ip_len;
    __u16 tcp_len;
    __u16 window;
    __u8 ip_hdr_len;
    __u8 tcp_hdr_len;
    __u8 ttl;
    __u8 dscp;
    __u8 ecn;
    __u8 tcp_flags;
    __u8 payload_prefix_len;
    __u8 payload_prefix[MAX_PAYLOAD_PREFIX];
};

/*
 * packet_event_t 구조체 설명(주요 필드):
 * - ts_ns: 커널의 monotonic timestamp (나노초)
 * - ifindex: 패킷이 수신된 인터페이스 인덱스
 * - frame_len: 프레임(이더넷) 전체 길이
 * - saddr/daddr: IPv4 소스/목적지 주소 (네트워크 바이트 오더)
 * - seq/ack_seq: TCP 시퀀스/ACK 필드
 * - sport/dport: TCP 포트 (호스트 오더로 전달하지 않음 — 사용자 공간에서 변환)
 * - ip_len, tcp_len: IP/TCP 길이 정보
 * - tcp_flags: TCP 플래그 바이트
 * - payload_prefix_len: payload_prefix에 유효한 바이트 수
 * - payload_prefix: 페이로드 앞부분 (최대 MAX_PAYLOAD_PREFIX)
 *
 * 이 구조체는 perf 이벤트로 출력되어 Python 프로세스가 해석합니다.
 */

BPF_PERF_OUTPUT(events);

int xdp_tg_capture(struct xdp_md *ctx)
{
    /*
     * xdp 프로그램의 진입점입니다. ctx로부터 패킷 데이터의 시작/끝 포인터를 얻고
     * 여러 유효성 검사를 순차적으로 수행합니다. 검사에 실패하면 XDP_PASS로
     * 패킷을 커널 네트워킹 스택에 넘깁니다.
     */
    void *data = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;
    /* 진입 훅 카운트 증가 */
    count_stat(STAT_HOOK);

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;

    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;
    count_stat(STAT_ETH_IP);

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;

    if (ip->protocol != IPPROTO_TCP)
        return XDP_PASS;
    /* IP 패킷이고 TCP가 아니면 빠져나감 */
    count_stat(STAT_IP_TCP);

    __u8 ip_hdr_len = (((__u8 *)ip)[0] & 0x0f) * 4;
    if (ip_hdr_len < 20 || ip_hdr_len > 60)
        return XDP_PASS;

    struct tcphdr *tcp = (void *)ip + ip_hdr_len;
    if ((void *)(tcp + 1) > data_end)
        return XDP_PASS;

    __u8 tcp_hdr_len = (((__u8 *)tcp)[12] >> 4) * 4;
    if (tcp_hdr_len < 20 || tcp_hdr_len > 60)
        return XDP_PASS;
    count_stat(STAT_TCP_HEADER);

    void *payload = (void *)tcp + tcp_hdr_len;
    if (payload > data_end)
        return XDP_PASS;

    __u16 ip_len = bpf_ntohs(ip->tot_len);
    if (ip_len < ip_hdr_len + tcp_hdr_len)
        return XDP_PASS;

    __u16 tcp_len = ip_len - ip_hdr_len - tcp_hdr_len;
    if (tcp_len == 0)
        count_stat(STAT_TCP_ACK_ONLY);
    else
        count_stat(STAT_TCP_PAYLOAD);

    struct packet_event_t event = {};
    event.ts_ns = bpf_ktime_get_ns();
    event.ifindex = ctx->ingress_ifindex;
    event.frame_len = data_end - data;
    event.saddr = ip->saddr;
    event.daddr = ip->daddr;
    event.sport = bpf_ntohs(tcp->source);
    event.dport = bpf_ntohs(tcp->dest);
    event.ip_len = ip_len;
    event.ip_hdr_len = ip_hdr_len;
    event.ttl = ip->ttl;
    event.dscp = ip->tos >> 2;
    event.ecn = ip->tos & 0x3;
    event.tcp_len = tcp_len;
    event.tcp_hdr_len = tcp_hdr_len;
    event.seq = bpf_ntohl(tcp->seq);
    event.ack_seq = bpf_ntohl(tcp->ack_seq);
    event.window = bpf_ntohs(tcp->window);
    event.tcp_flags = ((__u8 *)tcp)[13];

    __u32 payload_offset = sizeof(*eth) + ip_hdr_len + tcp_hdr_len;
    if (tcp_len >= MAX_PAYLOAD_PREFIX &&
        bpf_xdp_load_bytes(ctx, payload_offset, event.payload_prefix, MAX_PAYLOAD_PREFIX) == 0) {
        event.payload_prefix_len = MAX_PAYLOAD_PREFIX;
    } else if (tcp_len >= 20 &&
        bpf_xdp_load_bytes(ctx, payload_offset, event.payload_prefix, 20) == 0) {
        event.payload_prefix_len = 20;
    }

    /* 추출한 이벤트를 perf 버퍼로 사용자 공간에 제출 */
    events.perf_submit(ctx, &event, sizeof(event));
    /* 제출 카운트 증가 */
    count_stat(STAT_SUBMIT);
    return XDP_PASS;
}
"""


# Python 쪽에서 C의 packet_event_t와 매칭되는 ctypes 구조체입니다.
# 이 구조체는 BPF가 perf 이벤트로 전송한 바이너리 레이아웃을 그대로 표현합니다.
# 필드 타입과 순서는 C 구조체와 정확히 일치해야 하며, 바이트 오더에 유의해야 합니다.
class BpfPacketEvent(ct.Structure):
    _fields_ = [
        ("ts_ns", ct.c_ulonglong),
        ("ifindex", ct.c_uint),
        ("frame_len", ct.c_uint),
        ("saddr", ct.c_uint),
        ("daddr", ct.c_uint),
        ("seq", ct.c_uint),
        ("ack_seq", ct.c_uint),
        ("sport", ct.c_ushort),
        ("dport", ct.c_ushort),
        ("ip_len", ct.c_ushort),
        ("tcp_len", ct.c_ushort),
        ("window", ct.c_ushort),
        ("ip_hdr_len", ct.c_ubyte),
        ("tcp_hdr_len", ct.c_ubyte),
        ("ttl", ct.c_ubyte),
        ("dscp", ct.c_ubyte),
        ("ecn", ct.c_ubyte),
        ("tcp_flags", ct.c_ubyte),
        ("payload_prefix_len", ct.c_ubyte),
        ("payload_prefix", ct.c_ubyte * MAX_PAYLOAD_PREFIX),
    ]


XDP_FLAGS = {
    "native": 1 << 0,
    "skb": 1 << 1,
    "hw": 1 << 3,
}

KERNEL_STAT_NAMES = [
    "hook",
    "eth_ip",
    "ip_tcp",
    "tcp_header",
    "tcp_payload",
    "submit",
    "tcp_ack_only",
]


def ip_to_str(value: int) -> str:
    return socket.inet_ntoa(struct.pack("I", value))


def make_event(
    raw: BpfPacketEvent,
    frame_number: int,
    ifname_by_index: dict[int, str],
    monotonic_to_epoch_offset_ns: int,
) -> XdpPacketEvent:
    """커널 perf event 구조체를 Python 파이프라인이 쓰는 XdpPacketEvent로 바꾼다.

    설명:
    - raw: ctypes로 해석한 BPF의 packet_event_t 구조체
    - frame_number: 이 프로세스가 받은 순번(Perf 이벤트 순서)
    - ifname_by_index: 인터페이스 인덱스 -> 이름 매핑
    - monotonic_to_epoch_offset_ns: 커널의 monotonic 타임스탬프를 epoch로 변환하기 위한 오프셋

    반환값은 pipeline에서 사용하는 XdpPacketEvent 데이터 클래스입니다.
    """
    prefix_len = int(raw.payload_prefix_len)
    payload_prefix = bytes(raw.payload_prefix[:prefix_len])
    ifname = ifname_by_index.get(int(raw.ifindex), f"ifindex-{int(raw.ifindex)}")
    epoch_ts_ns = int(raw.ts_ns) + monotonic_to_epoch_offset_ns
    return XdpPacketEvent(
        ts_us=int(raw.ts_ns // 1000),
        epoch_ts_us=int(epoch_ts_ns // 1000),
        frame_number=frame_number,
        ifname=ifname,
        src_ip=ip_to_str(raw.saddr),
        dst_ip=ip_to_str(raw.daddr),
        src_port=int(raw.sport),
        dst_port=int(raw.dport),
        frame_len=int(raw.frame_len),
        ip_len=int(raw.ip_len),
        ip_hdr_len=int(raw.ip_hdr_len),
        ip_ttl=int(raw.ttl),
        ip_dscp=int(raw.dscp),
        ip_ecn=int(raw.ecn),
        tcp_len=int(raw.tcp_len),
        tcp_hdr_len=int(raw.tcp_hdr_len),
        tcp_seq=int(raw.seq),
        tcp_ack=int(raw.ack_seq),
        tcp_flags=int(raw.tcp_flags),
        tcp_window_size=int(raw.window),
        payload_prefix=payload_prefix,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XDP로 TrafficGenerator TCP 패킷을 캡처한다.")
    parser.add_argument("-i", "--interface", nargs="+", required=True)
    parser.add_argument("--xdp-mode", choices=sorted(XDP_FLAGS), default="skb")
    parser.add_argument("--feature-packet-count", type=int, default=10)
    parser.add_argument("--server-port", type=int, default=5001)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument(
        "--print-ready",
        action="store_true",
        help="flow가 feature-packet-count에 도달할 때마다 출력한다",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--redis-stream", default="flow_features")
    parser.add_argument("--redis-stream-maxlen", type=int, default=None)
    parser.add_argument(
        "--publish-direction",
        choices=["src_to_dst", "dst_to_src", "all"],
        default="dst_to_src",
        help="Redis Stream으로 보낼 ready flow 방향",
    )
    return parser.parse_args()


def main() -> int:
    # 프로그램 진입점: 인자 파싱, BPF 컴파일/로딩, perf 버퍼 핸들러 등록, 메인 루프 실행
    args = parse_args()
    interfaces = list(dict.fromkeys(args.interface))
    ifname_by_index = {
        socket.if_nametoindex(ifname): ifname
        for ifname in interfaces
    }
    src_index_by_ip = build_default_src_index_by_ip(k=args.k)
    online_cache = TrafficGeneratorOnlineFlowCache(
        feature_packet_count=args.feature_packet_count,
        src_index_by_ip=src_index_by_ip,
        server_port=args.server_port,
    )
    # bpf_ktime_get_ns()는 monotonic 계열 timestamp다. Redis/분석은 epoch wall-clock을
    # 쓰므로 user-space에서 측정한 offset으로 packet epoch timestamp를 함께 기록한다.
    monotonic_to_epoch_offset_ns = time.time_ns() - time.monotonic_ns()

    bpf = BPF(text=BPF_PROGRAM)
    fn = bpf.load_func("xdp_tg_capture", BPF.XDP)
    flags = XDP_FLAGS[args.xdp_mode]
    frame_number = 0
    stats = {
        "events": 0,
        "metadata": 0,
        "matched": 0,
        "ready": 0,
        "published": 0,
        "key_errors": 0,
        "publish_errors": 0,
    }
    direction_counts = {
        "src_to_dst": 0,
        "dst_to_src": 0,
        "unknown": 0,
    }
    stream_producer = (
        RedisStreamProducer(
            redis_url=args.redis_url,
            stream_name=args.redis_stream,
            maxlen=args.redis_stream_maxlen,
        )
        if args.redis_url
        else None
    )

    def should_publish(entry) -> bool:
        return args.publish_direction == "all" or entry.direction == args.publish_direction

    def on_event(cpu: int, data: int, size: int) -> None:
        """perf 버퍼로부터 전달된 패킷 이벤트를 처리한다.

        처리 흐름:
        1) perf로 받은 바이너리를 ctypes 구조체로 변환
        2) make_event로 의미 있는 Python 객체로 변환
        3) 패킷 방향(src/dst) 집계
        4) TrafficGenerator 메타데이터 파싱
        5) online_cache에 이벤트를 전달하여 flow 매칭/ready 상태 판단
        6) ready한 flow는 로그 출력 및 (옵션) Redis Stream에 publish
        7) 오류는 stderr에 기록하고 통계를 갱신
        """
        nonlocal frame_number
        frame_number += 1
        stats["events"] += 1
        raw = ct.cast(data, ct.POINTER(BpfPacketEvent)).contents
        event = make_event(
            raw,
            frame_number=frame_number,
            ifname_by_index=ifname_by_index,
            monotonic_to_epoch_offset_ns=monotonic_to_epoch_offset_ns,
        )
        if event.dst_port == args.server_port:
            direction_counts["src_to_dst"] += 1
        elif event.src_port == args.server_port:
            direction_counts["dst_to_src"] += 1
        else:
            direction_counts["unknown"] += 1

        if parse_tg_metadata(event.payload_prefix) is not None:
            stats["metadata"] += 1

        try:
            entry = online_cache.process_event(event)
        except KeyError as exc:
            stats["key_errors"] += 1
            print(f"[xdp] skip packet: {exc}", file=sys.stderr)
            return

        if entry is not None:
            stats["matched"] += 1

        if entry is not None and online_cache.flow_cache.is_ready(entry):
            stats["ready"] += 1
            if args.print_ready:
                tcp_lens = [pkt.tcp_len for pkt in entry.packets]
                print(
                    "[ready] src_index=%s flow_id=%s direction=%s packets=%s payload_bytes=%s tcp_lens=%s"
                    % (
                        entry.src_index,
                        entry.flow_id,
                        entry.direction,
                        len(entry.packets),
                        entry.payload_bytes,
                        tcp_lens,
                    )
                )

            if stream_producer is not None and should_publish(entry):
                try:
                    request = build_online_flow_request(
                        entry,
                        packet_count=args.feature_packet_count,
                        run_id=args.run_id,
                        capture_mode="xdp",
                        feature_ready_wall_ns=time.time_ns(),
                    )
                    stream_id = stream_producer.publish(request)
                    stats["published"] += 1
                    if args.print_ready:
                        print(
                            "[stream] id=%s stream=%s src_index=%s flow_id=%s direction=%s"
                            % (
                                stream_id,
                                args.redis_stream,
                                entry.src_index,
                                entry.flow_id,
                                entry.direction,
                            )
                        )
                except Exception as exc:
                    stats["publish_errors"] += 1
                    print(f"[redis] stream publish failed: {exc}", file=sys.stderr)
                    return

            online_cache.mark_pending(entry)

    for ifname in interfaces:
        bpf.attach_xdp(ifname, fn, flags)

    bpf["events"].open_perf_buffer(on_event)
    print(
        "[*] XDP TrafficGenerator capture attached to %s (%s mode)"
        % (", ".join(interfaces), args.xdp_mode)
    )
    if stream_producer is not None:
        stream_producer.connect()
        print(
            "[*] Redis Stream publishing enabled (stream=%s, direction=%s)"
            % (args.redis_stream, args.publish_direction)
        )
    print("[*] Press Ctrl-C to stop")

    try:
        while True:
            bpf.perf_buffer_poll(timeout=100)
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        for ifname in interfaces:
            bpf.remove_xdp(ifname, flags)
        if stream_producer is not None:
            stream_producer.close()
        kernel_stats = {}
        stats_map = bpf["kernel_stats"]
        for index, name in enumerate(KERNEL_STAT_NAMES):
            kernel_stats[name] = int(stats_map[ct.c_int(index)].value)
        print(
            "[summary] events=%s metadata=%s matched=%s ready=%s published=%s "
            "key_errors=%s publish_errors=%s "
            "directions(src_to_dst=%s,dst_to_src=%s,unknown=%s)"
            % (
                stats["events"],
                stats["metadata"],
                stats["matched"],
                stats["ready"],
                stats["published"],
                stats["key_errors"],
                stats["publish_errors"],
                direction_counts["src_to_dst"],
                direction_counts["dst_to_src"],
                direction_counts["unknown"],
            )
        )
        print(
            "[kernel] hook=%s eth_ip=%s ip_tcp=%s tcp_header=%s tcp_payload=%s submit=%s"
            " tcp_ack_only=%s"
            % (
                kernel_stats["hook"],
                kernel_stats["eth_ip"],
                kernel_stats["ip_tcp"],
                kernel_stats["tcp_header"],
                kernel_stats["tcp_payload"],
                kernel_stats["submit"],
                kernel_stats["tcp_ack_only"],
            )
        )
        print("[*] XDP detached")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
