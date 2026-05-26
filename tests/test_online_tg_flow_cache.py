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
