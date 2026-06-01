# Third-Party Notices

This repository includes third-party materials. Unless explicitly stated
otherwise, the project license in `LICENSE` applies only to code and
documentation authored by the Capstone Online Flow Classification contributors.

## TrafficGenerator

This project uses the HKUST-SING TrafficGenerator as an external dependency.
The TrafficGenerator code is not vendored in this repository.

Original source:

```text
https://github.com/HKUST-SING/TrafficGenerator
```

The tool is associated with the following publication:

```text
Enabling ECN in Multi-Service Multi-Queue Data Centers
Wei Bai, Li Chen, Kai Chen, Haitao Wu
USENIX NSDI 2016
```

Please use the citation requested by the upstream project when referring to this
traffic generator in academic work.

The TrafficGenerator code is not relicensed by this project. Its original
copyright, license, and redistribution terms, if any, remain with the original
authors or source. Before redistributing or reusing TrafficGenerator
independently, verify the applicable permissions from the original source or
authors.

This project expects local TrafficGenerator modifications for online flow
classification experiments. See `docs/TRAFFIC_GENERATOR_MODIFICATIONS.md` for a
summary.
