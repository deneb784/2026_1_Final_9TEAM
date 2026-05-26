import struct
import unittest

from pipeline.realtime.online_tg_flow_cache import (
    TG_FLOW_DIR_DST_TO_SRC,
    TG_FLOW_DIR_SRC_TO_DST,
    TrafficGeneratorOnlineFlowCache,
    XdpPacketEvent,
    parse_tg_metadata,
)


def tg_meta(flow_id: int, size_bytes: int, tos: int, rate_mbps: int, direction: int) -> bytes:
    # 실제 TrafficGenerator가 TCP payload 앞에 붙이는 20바이트 metadata를 테스트에서 재현한다.
    # 이 bytes가 있어야 online_tg_flow_cache가 "새 논리 flow가 시작됐다"고 판단한다.
    return struct.pack("<IIIII", flow_id, size_bytes, tos, rate_mbps, direction)


def event(
    *,
    ts_us: int,
    frame_number: int,
    src_ip: str,
    dst_ip: str,
    src_port: int,
    dst_port: int,
    tcp_len: int,
    payload_prefix: bytes = b"",
    seq: int = 1,
    tcp_flags: int | None = None,
) -> XdpPacketEvent:
    # 테스트용 XDP 이벤트 생성 헬퍼.
    # tcp_len이 있으면 PSH+ACK(0x18), payload가 없으면 ACK-only(0x10)로 기본 플래그를 맞춘다.
    if tcp_flags is None:
        tcp_flags = 0x18 if tcp_len else 0x10

    return XdpPacketEvent(
        ts_us=ts_us,
        epoch_ts_us=None,
        frame_number=frame_number,
        ifname="edge_p0_e0-eth1",
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=src_port,
        dst_port=dst_port,
        frame_len=14 + 20 + 20 + tcp_len,
        ip_len=20 + 20 + tcp_len,
        ip_hdr_len=20,
        ip_ttl=64,
        ip_dscp=0,
        ip_ecn=0,
        tcp_len=tcp_len,
        tcp_hdr_len=20,
        tcp_seq=seq,
        tcp_ack=1,
        tcp_flags=tcp_flags,
        tcp_window_size=1024,
        payload_prefix=payload_prefix,
    )


class TrafficGeneratorOnlineFlowCacheTest(unittest.TestCase):
    def test_parse_tg_metadata(self):
        # payload prefix에서 flow_id/크기/방향을 정확히 읽어야 이후 FlowCache key를 만들 수 있다.
        parsed = parse_tg_metadata(tg_meta(7, 1234, 5, 80, TG_FLOW_DIR_DST_TO_SRC))

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.flow_id, 7)
        self.assertEqual(parsed.size_bytes, 1234)
        self.assertEqual(parsed.tos, 5)
        self.assertEqual(parsed.rate_mbps, 80)
        self.assertEqual(parsed.direction, "dst_to_src")

    def test_persistent_connection_dst_to_src_flows_are_separated_by_tg_metadata(self):
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=2,
            src_index_by_ip={"10.0.0.1": 0},
        )

        # 같은 TCP connection에서 첫 번째 TrafficGenerator 응답 flow가 시작된다.
        # persistent connection에서는 5-tuple이 계속 같으므로, metadata의 flow_id가 실제 request 경계다.
        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                payload_prefix=tg_meta(1, 200, 1, 80, TG_FLOW_DIR_DST_TO_SRC) + b"x" * 12,
                seq=100,
            )
        )
        cache.process_event(
            event(
                ts_us=1_100,
                frame_number=2,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=100,
                seq=220,
            )
        )

        # 같은 5-tuple이지만 metadata의 flow_id가 바뀌면 두 번째 flow로 분리되어야 한다.
        # 이 분리가 안 되면 서로 다른 request의 패킷이 한 모델 입력에 섞인다.
        cache.process_event(
            event(
                ts_us=2_000,
                frame_number=3,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=70,
                payload_prefix=tg_meta(2, 50, 1, 80, TG_FLOW_DIR_DST_TO_SRC) + b"y" * 12,
                seq=320,
            )
        )

        first = cache.flow_cache.entries[(0, 1, "dst_to_src")]
        second = cache.flow_cache.entries[(0, 2, "dst_to_src")]

        self.assertEqual([pkt.frame_number for pkt in first.packets], [1, 2])
        self.assertEqual([pkt.frame_number for pkt in second.packets], [3])
        self.assertEqual(first.status, "default")
        self.assertEqual(second.status, "default")
        self.assertTrue(cache.flow_cache.is_ready(first))
        self.assertFalse(cache.flow_cache.is_ready(second))

    def test_src_to_dst_request_metadata_creates_src_flow_entry(self):
        # client -> server 방향 payload에 TG metadata가 있으면 src_to_dst entry가 만들어져야 한다.
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=1,
            src_index_by_ip={"10.0.0.1": 0},
        )

        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.0.0.1",
                dst_ip="10.2.0.1",
                src_port=40000,
                dst_port=5001,
                tcp_len=20,
                payload_prefix=tg_meta(3, 1000, 0, 80, TG_FLOW_DIR_SRC_TO_DST),
            )
        )

        entry = cache.flow_cache.entries[(0, 3, "src_to_dst")]
        self.assertEqual(entry.logical_flow_id, "0:3:src_to_dst")
        self.assertEqual(entry.payload_bytes, 20)
        self.assertEqual(entry.status, "default")
        self.assertTrue(cache.flow_cache.is_ready(entry))

    def test_ready_flow_transitions_to_pending(self):
        # ready entry를 Redis Stream으로 보낸 뒤에는 결과 대기 상태(pending)로 바뀌어야 한다.
        # pending 상태는 같은 flow를 중복 publish하지 않기 위한 잠금 역할을 한다.
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=1,
            src_index_by_ip={"10.0.0.1": 0},
        )

        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                payload_prefix=tg_meta(1, 200, 1, 80, TG_FLOW_DIR_DST_TO_SRC),
            )
        )

        entry = cache.flow_cache.entries[(0, 1, "dst_to_src")]
        self.assertEqual(entry.status, "default")
        self.assertTrue(cache.flow_cache.is_ready(entry))

        cache.mark_pending(entry)

        self.assertEqual(entry.status, "pending")
        self.assertFalse(cache.flow_cache.is_ready(entry))

    def test_pending_flow_does_not_collect_more_packets(self):
        # feature를 이미 Stream에 올린 flow가 계속 패킷을 더 모으면,
        # worker가 받은 request payload와 cache 내부 상태가 달라질 수 있다.
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=1,
            src_index_by_ip={"10.0.0.1": 0},
        )

        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                payload_prefix=tg_meta(1, 300, 1, 80, TG_FLOW_DIR_DST_TO_SRC),
                seq=100,
            )
        )
        entry = cache.flow_cache.entries[(0, 1, "dst_to_src")]
        cache.mark_pending(entry)

        cache.process_event(
            event(
                ts_us=1_010,
                frame_number=2,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                seq=220,
            )
        )

        self.assertEqual([pkt.frame_number for pkt in entry.packets], [1])
        self.assertEqual(entry.payload_bytes, 120)

    def test_ack_only_packet_is_skipped_in_active_flow(self):
        # ACK-only 패킷은 tcp_len이 0이라 모델 feature로 쓰지 않는다.
        # active flow 안에 있어도 payload_bytes와 packets에 추가되지 않아야 한다.
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=2,
            src_index_by_ip={"10.0.0.1": 0},
        )

        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                payload_prefix=tg_meta(1, 200, 1, 80, TG_FLOW_DIR_DST_TO_SRC),
            )
        )
        cache.process_event(
            event(
                ts_us=1_010,
                frame_number=2,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=0,
            )
        )

        entry = cache.flow_cache.entries[(0, 1, "dst_to_src")]
        self.assertEqual([pkt.frame_number for pkt in entry.packets], [1])
        self.assertEqual([pkt.tcp_len for pkt in entry.packets], [120])
        self.assertEqual(entry.payload_bytes, 120)
        self.assertFalse(cache.flow_cache.is_ready(entry))

    def test_response_ack_only_before_response_metadata_is_skipped(self):
        # 요청(src_to_dst) 뒤에 응답 ACK-only가 먼저 보일 수 있다.
        # 아직 dst_to_src metadata를 못 봤다면 응답 flow_id를 알 수 없으므로 그 ACK는 버린다.
        cache = TrafficGeneratorOnlineFlowCache(
            feature_packet_count=2,
            src_index_by_ip={"10.0.0.1": 0},
        )

        cache.process_event(
            event(
                ts_us=1_000,
                frame_number=1,
                src_ip="10.0.0.1",
                dst_ip="10.2.0.1",
                src_port=40000,
                dst_port=5001,
                tcp_len=20,
                payload_prefix=tg_meta(4, 200, 1, 80, TG_FLOW_DIR_SRC_TO_DST),
            )
        )
        cache.process_event(
            event(
                ts_us=1_010,
                frame_number=2,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=0,
            )
        )
        cache.process_event(
            event(
                ts_us=1_020,
                frame_number=3,
                src_ip="10.2.0.1",
                dst_ip="10.0.0.1",
                src_port=5001,
                dst_port=40000,
                tcp_len=120,
                payload_prefix=tg_meta(4, 200, 1, 80, TG_FLOW_DIR_DST_TO_SRC),
            )
        )

        entry = cache.flow_cache.entries[(0, 4, "dst_to_src")]
        self.assertEqual([pkt.frame_number for pkt in entry.packets], [3])
        self.assertEqual([pkt.tcp_len for pkt in entry.packets], [120])
        self.assertEqual(entry.payload_bytes, 120)


if __name__ == "__main__":
    unittest.main()
