import unittest

from feature_pipeline.online_request import build_online_flow_request
from feature_pipeline.models import FlowEntry, PacketRecord


def packet(frame_number: int, ts_us: int, tcp_len: int) -> PacketRecord:
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


class OnlineRequestTest(unittest.TestCase):
    def test_build_online_flow_request_uses_dataset_feature_shape(self):
        entry = FlowEntry(
            src_index=0,
            flow_id=7,
            direction="dst_to_src",
            logical_flow_id="0:7:dst_to_src",
        )
        entry.packets.extend([
            packet(frame_number=1, ts_us=1_000, tcp_len=0),
            packet(frame_number=2, ts_us=1_010, tcp_len=1448),
        ])
        entry.payload_bytes = 1448

        request = build_online_flow_request(entry, packet_count=3, run_id="unit")

        self.assertEqual(request["request_key"], {
            "src_index": 0,
            "flow_id": 7,
            "direction": "dst_to_src",
        })
        self.assertEqual(request["logical_flow_id"], "0:7:dst_to_src")
        self.assertEqual(request["seq_len"], 2)
        self.assertEqual(len(request["x"]), 3)
        self.assertEqual(len(request["x"][0]), 18)
        self.assertEqual(request["x"][0][0], 1)
        self.assertEqual(request["x"][1][15], 10)
        self.assertEqual(request["x"][1][17], 1448)
        self.assertEqual(request["run_id"], "unit")


if __name__ == "__main__":
    unittest.main()
