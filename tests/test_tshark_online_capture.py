import struct
import unittest

from pipeline.realtime.online_tg_flow_cache import parse_tg_metadata
from pipeline.realtime.tg_tshark_capture import FIELDS, parse_tshark_line


def tg_meta(flow_id: int, size_bytes: int, tos: int, rate_mbps: int, direction: int) -> str:
    return struct.pack("<IIIII", flow_id, size_bytes, tos, rate_mbps, direction).hex(":")


class TsharkOnlineCaptureTest(unittest.TestCase):
    def test_parse_tshark_line_builds_online_event_with_payload_prefix(self):
        values = {
            "frame.number": "7",
            "frame.time_epoch": "1700000000.123456",
            "frame.len": "94",
            "ip.src": "10.2.0.1",
            "ip.dst": "10.0.0.1",
            "ip.len": "80",
            "ip.hdr_len": "20",
            "ip.ttl": "64",
            "ip.dsfield.dscp": "0",
            "ip.dsfield.ecn": "0",
            "tcp.srcport": "5001",
            "tcp.dstport": "40000",
            "tcp.len": "40",
            "tcp.hdr_len": "20",
            "tcp.seq": "1",
            "tcp.ack": "2",
            "tcp.flags": "0x0018",
            "tcp.window_size": "1024",
            "tcp.payload": tg_meta(3, 1234, 1, 80, 1) + ":aa:bb:cc:dd",
        }
        line = "\t".join(values.get(field, "") for field in FIELDS)

        event = parse_tshark_line(line, "h1-eth0", fallback_frame_number=1)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.epoch_ts_us, 1_700_000_000_123_456)
        self.assertEqual(event.ifname, "h1-eth0")
        self.assertEqual(event.src_port, 5001)
        self.assertEqual(event.dst_port, 40000)
        self.assertEqual(event.tcp_flags, 0x18)
        parsed = parse_tg_metadata(event.payload_prefix)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.flow_id, 3)
        self.assertEqual(parsed.direction, "dst_to_src")


if __name__ == "__main__":
    unittest.main()
