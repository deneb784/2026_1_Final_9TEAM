import unittest

from pipeline.realtime.online_request import build_online_flow_request
from pipeline.realtime.online_flow_cache import OnlineFlowEntry, OnlineFlowKey
from pipeline.models import PacketRecord


def packet(frame_number: int, ts_us: int, tcp_len: int) -> PacketRecord:
    # build_online_flow_request는 OnlineFlowEntry 안의 PacketRecord를 dataset feature로 변환한다.
    # 테스트에서는 pcap을 읽지 않고 필요한 필드만 채운 PacketRecord를 직접 만든다.
    return PacketRecord(
        source_file="h1-eth0",
        frame_number=frame_number,
        ts_us=ts_us,
        src_ip="10.2.0.1",
        dst_ip="10.0.0.1",
        src_port=5001,
        dst_port=40000,
        frame_len=14 + 20 + 20 + tcp_len,
        ip_len=20 + 20 + tcp_len,
        ip_hdr_len=20,
        ip_ttl=64,
        ip_dscp=0,
        ip_ecn=0,
        tcp_stream=0,
        tcp_len=tcp_len,
        tcp_hdr_len=20,
        tcp_seq=frame_number,
        tcp_ack=1,
        tcp_flags="0x018",
        tcp_syn=0,
        tcp_ack_flag=1,
        tcp_psh=1 if tcp_len else 0,
        tcp_fin=0,
        tcp_rst=0,
        tcp_window_size=1024,
        tcp_time_relative=0.0,
        tcp_time_delta=0.0,
        retransmission=False,
        out_of_order=False,
        duplicate_ack=False,
        fast_retransmission=False,
    )


def online_entry(flow_id: int = 7, direction: str = "dst_to_src") -> OnlineFlowEntry:
    return OnlineFlowEntry(
        key=OnlineFlowKey(
            client_ip="10.0.0.1",
            client_port=40000,
            server_ip="10.2.0.1",
            server_port=5001,
            flow_id=flow_id,
            direction=direction,
        )
    )


class OnlineRequestTest(unittest.TestCase):
    def test_build_online_flow_request_uses_dataset_feature_shape(self):
        # Redis Stream에 실을 request payload가 모델 worker가 기대하는 feature shape를 갖는지 확인한다.
        # ACK-only 패킷(tcp_len=0)은 입력 feature에서 제외되고, payload 패킷만 seq_len에 반영된다.
        entry = online_entry()
        entry.packets.extend([
            packet(frame_number=1, ts_us=1_000, tcp_len=0),
            packet(frame_number=2, ts_us=1_010, tcp_len=1448),
        ])
        entry.payload_bytes = 1448

        request = build_online_flow_request(entry, packet_count=3, run_id="unit")

        self.assertEqual(request["online_flow_key"], entry.online_flow_key)
        self.assertNotIn("request_key", request)
        self.assertEqual(
            request["logical_flow_id"],
            "10.0.0.1:40000->10.2.0.1:5001:7:dst_to_src",
        )
        self.assertEqual(request["seq_len"], 1)
        self.assertEqual(len(request["x"]), 3)
        self.assertEqual(len(request["x"][0]), 18)
        self.assertEqual(request["x"][0][0], 1)
        self.assertEqual(request["x"][0][7], 1448)
        self.assertEqual(request["x"][0][15], 0)
        self.assertEqual(request["x"][0][17], 1448)
        self.assertEqual(request["run_id"], "unit")
        self.assertEqual(request["producer_metrics"]["capture_mode"], None)
        self.assertEqual(request["producer_metrics"]["first_packet_ts_us"], 1_010)
        self.assertEqual(request["producer_metrics"]["last_packet_ts_us"], 1_010)
        self.assertEqual(request["producer_metrics"]["feature_packet_count_observed"], 1)
        self.assertIsInstance(request["producer_metrics"]["feature_ready_wall_ns"], int)

    def test_build_online_flow_request_accepts_latency_metadata(self):
        # transport.py는 producer_metrics 일부를 Stream field로 복사한다.
        # 여기서는 capture_mode와 feature_ready_wall_ns가 request에 보존되는지 확인한다.
        entry = online_entry()
        entry.packets.append(packet(frame_number=1, ts_us=1_000, tcp_len=1448))
        entry.payload_bytes = 1448

        request = build_online_flow_request(
            entry,
            packet_count=3,
            run_id="unit",
            capture_mode="xdp",
            feature_ready_wall_ns=123,
        )

        self.assertEqual(request["producer_metrics"]["capture_mode"], "xdp")
        self.assertEqual(request["producer_metrics"]["feature_ready_wall_ns"], 123)

    def test_build_online_flow_request_prefers_packet_epoch_timestamp(self):
        # XDP 온라인 경로는 wall-clock epoch timestamp를 줄 수 있다.
        # latency 계산에는 상대 ts_us보다 epoch_ts_us가 더 직접적이므로 우선 사용한다.
        entry = online_entry()
        pkt = packet(frame_number=1, ts_us=1_000, tcp_len=1448)
        pkt.epoch_ts_us = 1_700_000_000_000_000
        entry.packets.append(pkt)

        request = build_online_flow_request(entry, packet_count=3)

        self.assertEqual(
            request["producer_metrics"]["first_packet_ts_us"],
            1_700_000_000_000_000,
        )
        self.assertEqual(
            request["producer_metrics"]["last_packet_ts_us"],
            1_700_000_000_000_000,
        )

    def test_build_online_flow_request_keeps_online_flow_key(self):
        key = OnlineFlowKey(
            client_ip="10.0.0.1",
            client_port=40000,
            server_ip="10.2.0.1",
            server_port=5001,
            flow_id=7,
            direction="dst_to_src",
        )
        entry = OnlineFlowEntry(key=key)
        entry.packets.append(packet(frame_number=1, ts_us=1_000, tcp_len=1448))
        entry.payload_bytes = 1448

        request = build_online_flow_request(entry, packet_count=3)

        self.assertEqual(request["online_flow_key"], key.as_dict())
        self.assertNotIn("request_key", request)


if __name__ == "__main__":
    unittest.main()
