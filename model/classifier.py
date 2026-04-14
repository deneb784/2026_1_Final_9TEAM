import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, Any


class FlowClassifier(nn.Module):
    """플로우 특징을 기반으로 트래픽을 분류하는 신경망 모델"""

    def __init__(self, input_size: int = 14, hidden_size: int = 64, num_classes: int = 2):
        """
        Args:
            input_size: 입력 특징 수 (기본 14개: 패킷 수, 바이트, IAT 등)
            hidden_size: 은닉층 크기
            num_classes: 분류 클래스 수 (예: 정상/비정상)
        """
        super(FlowClassifier, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size // 2)
        self.fc3 = nn.Linear(hidden_size // 2, num_classes)
        self.dropout = nn.Dropout(0.3)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """순전파

        Args:
            x: 입력 특징 텐서 (batch_size, input_size)

        Returns:
            분류 확률 (batch_size, num_classes)
        """
        x = F.relu(self.fc1(x))
        x = self.dropout(x)
        x = F.relu(self.fc2(x))
        x = self.dropout(x)
        x = self.fc3(x)
        return F.softmax(x, dim=1)


def load_model(model_path: str = "model_checkpoint.pth") -> FlowClassifier:
    """저장된 모델을 로드합니다.

    Args:
        model_path: 모델 체크포인트 파일 경로

    Returns:
        로드된 모델
    """
    model = FlowClassifier()
    try:
        model.load_state_dict(torch.load(model_path))
        model.eval()
        print(f"Model loaded from {model_path}")
    except FileNotFoundError:
        print(f"Warning: Model file {model_path} not found. Using untrained model.")
    return model


def classify_flow(model: FlowClassifier, features: Dict[str, Any]) -> Dict[str, Any]:
    """플로우 특징을 분류합니다.

    Args:
        model: 분류 모델
        features: 플로우 특징 딕셔너리

    Returns:
        분류 결과가 추가된 특징 딕셔너리
    """
    # 특징을 텐서로 변환 (feature_extractor.py의 특징 순서에 맞춤)
    feature_vector = torch.tensor([
        features.get("packet_count", 0),
        features.get("total_frame_bytes", 0),
        features.get("total_tcp_payload_bytes", 0),
        features.get("mean_frame_len", 0.0),
        features.get("min_frame_len", 0),
        features.get("max_frame_len", 0),
        features.get("flow_duration_us", 0),
        features.get("mean_iat_us", 0.0),
        features.get("min_iat_us", 0),
        features.get("max_iat_us", 0),
        features.get("retransmission_count", 0),
        features.get("duplicate_ack_count", 0),
        features.get("out_of_order_count", 0),
        features.get("fast_retransmission_count", 0),
    ], dtype=torch.float32).unsqueeze(0)  # 배치 차원 추가

    with torch.no_grad():
        outputs = model(feature_vector)
        probabilities = outputs.squeeze().tolist()

        # 가장 높은 확률의 클래스를 예측
        predicted_class = torch.argmax(outputs, dim=1).item()
        confidence = probabilities[predicted_class]

    # 결과 추가
    features["predicted_class"] = predicted_class
    features["confidence"] = confidence
    features["probabilities"] = probabilities

    return features


# 모델 저장을 위한 헬퍼 함수
def save_model(model: FlowClassifier, path: str = "model_checkpoint.pth"):
    """모델을 저장합니다."""
    torch.save(model.state_dict(), path)
    print(f"Model saved to {path}")


if __name__ == "__main__":
    # 모델 테스트
    model = FlowClassifier()
    print(model)

    # 더미 특징으로 테스트
    dummy_features = {
        "packet_count": 10,
        "total_frame_bytes": 1500,
        "total_tcp_payload_bytes": 1200,
        "mean_frame_len": 150.0,
        "min_frame_len": 60,
        "max_frame_len": 300,
        "flow_duration_us": 100000,
        "mean_iat_us": 10000.0,
        "min_iat_us": 1000,
        "max_iat_us": 20000,
        "retransmission_count": 0,
        "duplicate_ack_count": 0,
        "out_of_order_count": 0,
        "fast_retransmission_count": 0,
    }

    result = classify_flow(model, dummy_features)
    print(f"Classification result: {result}")

    # 모델 저장 (훈련되지 않은 상태로 저장)
    save_model(model, "model_checkpoint.pth")