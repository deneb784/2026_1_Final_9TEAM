from .data import DIRECTION_TO_INDEX, FeatureScaler
from .dataset_catalog import StepGruDatasetConfig, resolve_dataset_config

__all__ = [
    "DIRECTION_TO_INDEX",
    "DynamicPacketGRU",
    "FeatureScaler",
    "FlowClassifier",
    "StepGruDatasetConfig",
    "get_flow_stats",
    "load_model",
    "resolve_dataset_config",
]


def __getattr__(name: str):
    if name == "DynamicPacketGRU":
        from .models import DynamicPacketGRU

        return DynamicPacketGRU
    if name == "get_flow_stats":
        from .models import get_flow_stats

        return get_flow_stats
    if name in {"FlowClassifier", "load_model"}:
        from .inference import FlowClassifier, load_model

        return {"FlowClassifier": FlowClassifier, "load_model": load_model}[name]
    raise AttributeError(name)
