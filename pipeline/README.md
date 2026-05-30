# Pipeline Package Layout

`pipeline` is split by runtime responsibility.

```text
pipeline/
  models.py
  dataset/
  realtime/
  redis/
```

## Shared

```text
pipeline/models.py
```

Shared data models used by dataset and realtime pipelines.

## Dataset Generation

```text
pipeline/dataset/
  packet_loader.py
  meta_loader.py
  matcher.py
  flow_cache.py
  feature_extractor.py
  pipeline.py
  dataset_builder.py
  merge_mininet_runs.py
  relabel_dataset.py
  univ1_dataset_builder.py
```

Offline path for reading pcap/metadata files and building train/eval datasets.

## Realtime

```text
pipeline/realtime/
  online_flow_cache.py
  online_tg_flow_cache.py
  online_request.py
```

Online path for converting live packet events into realtime flow entries and model request payloads.

## Redis

```text
pipeline/redis/
  transport.py
  result_subscriber.py
```

Redis Stream/PubSub helpers used by the online pipeline.
