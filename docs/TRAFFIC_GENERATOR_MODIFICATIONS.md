# TrafficGenerator Local Modifications

This repository does not vendor the third-party TrafficGenerator source code.
For online flow classification experiments, the local TrafficGenerator checkout
must be modified to expose logical flow metadata in packet payloads.

Original source:

```text
https://github.com/HKUST-SING/TrafficGenerator
```

Publication reference:

```text
Enabling ECN in Multi-Service Multi-Queue Data Centers
Wei Bai, Li Chen, Kai Chen, Haitao Wu
USENIX NSDI 2016
```

Required local changes for this capstone pipeline:

- Add a 20-byte flow metadata header to request/response payloads.
- Include `flow_id`, `size`, `tos`, `rate`, and `direction` fields in that
  metadata.
- Define direction values for `src_to_dst` and `dst_to_src`.
- Preserve DSCP behavior while making the direction available to the online
  XDP/tshark capture pipeline.
- Echo response-side metadata from the server so `dst_to_src` flows can be
  reconstructed by `pipeline.realtime.online_tg_flow_cache`.
- Rebuild `TrafficGenerator/bin/client` and `TrafficGenerator/bin/server` after
  applying the local changes.
- Keep workload CDF files such as `DCTCP_CDF.txt`, `FB_CDF.txt`, and
  `VL2_CDF.txt` under the local `TrafficGenerator/conf/` directory.

The Python code in this repository assumes the local checkout is available at:

```text
TrafficGenerator/
```

The directory is intentionally ignored by git. Do not commit vendored
TrafficGenerator sources or binaries unless the upstream license/permission
status has been explicitly resolved.
