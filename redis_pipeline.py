import asyncio
from pipeline.dataset.pipeline import build_flow_request, find_pcap_files, serialize_flow_request
from pipeline.dataset.meta_loader import load_all_request_meta, build_meta_index
from pipeline.dataset.packet_loader import iter_packets_from_pcap
from pipeline.dataset.matcher import match_packet
from pipeline.flow_cache import FlowCache
from pipeline.dataset.feature_extractor import build_packet_features
from pipeline.redis.transport import RedisStreamProducer

async def run_pipeline_with_redis(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    feature_packet_count: int = 10,
    redis_host: str = "localhost",
    redis_port: int = 6379,
    redis_queue: str = "flow_features",
) -> list[str]:
    """Redis 연동된 비동기 파이프라인을 실행합니다."""
    redis_url = "redis://%s:%s/0" % (redis_host, redis_port)
    redis_producer = RedisStreamProducer(redis_url=redis_url, stream_name=redis_queue)
    redis_producer.connect()

    try:
        # 기존 pipeline 로직 재사용
        all_metas = load_all_request_meta(results_dir)
        meta_index = build_meta_index(all_metas)
        flow_cache = FlowCache(feature_packet_count=feature_packet_count)

        published_payloads: list[str] = []

        # PCAP 파일 찾기 (기존 함수 재사용)
        pcap_files = find_pcap_files(pcap_dir)

        for pcap_file in pcap_files:
            for packet in iter_packets_from_pcap(pcap_file):
                matched = match_packet(packet, meta_index)
                if matched is None:
                    continue

                meta, direction = matched
                entry = flow_cache.add_packet(meta, direction, packet)

                if flow_cache.is_ready(entry):
                    features = build_packet_features(entry)
                    flow_request = build_flow_request(meta, entry, features)
                    flow_request_json = serialize_flow_request(flow_request)

                    # Redis로 전송
                    redis_producer.publish(flow_request)
                    published_payloads.append(flow_request_json)

                    # 중복 전송 방지
                    flow_cache.mark_feature_sent(entry)

        return published_payloads

    finally:
        redis_producer.close()


def run_pipeline_sync_with_redis(
    results_dir: str = "results",
    pcap_dir: str = "captured_packet",
    feature_packet_count: int = 10,
    redis_host: str = "localhost",
    redis_port: int = 6379,
    redis_queue: str = "flow_features",
) -> list[str]:
    """동기 인터페이스로 Redis 연동 파이프라인을 실행합니다."""
    return asyncio.run(run_pipeline_with_redis(
        results_dir=results_dir,
        pcap_dir=pcap_dir,
        feature_packet_count=feature_packet_count,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_queue=redis_queue,
    ))


if __name__ == "__main__":
    payloads = run_pipeline_sync_with_redis(
        results_dir="results",
        pcap_dir="captured_packet",
        feature_packet_count=10,
    )
    print(f"Published {len(payloads)} flow requests to Redis queue")
