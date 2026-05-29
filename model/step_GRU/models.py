import torch
import torch.nn as nn

import json
import math
import numpy as np

class DynamicPacketGRU(nn.Module):
    def __init__(self, input_size=18, hidden_size=64): # 이전 데이터셋에 맞춰 input_size=18로 수정
        super(DynamicPacketGRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        
        # 1. 방향 정보를 초기 은닉 상태(h_0)로 변환하는 임베딩 레이어
        self.direction_embedding = nn.Embedding(num_embeddings=2, embedding_dim=hidden_size)
        
        # 2. 모델 내부 정규화
        self.layer_norm = nn.LayerNorm(input_size)
        
        # 3. GRU 셀
        self.gru_cell = nn.GRUCell(input_size=input_size, hidden_size=hidden_size)
        
        # 4. 출력층 (0~1 확률값)
        self.classifier = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, direction_idx, enable_early_exit=False, tolerance=0.05, max_packets=None):
        """
        x: 패킷 시퀀스 (batch_size, seq_len, input_size) 
        max_packets: 설정 시 해당 패킷 수까지만 연산하고 종료
        """
        batch_size, seq_len, _ = x.size()
        
        # max_packets가 설정되어 있고, 실제 seq_len보다 작다면 제한을 둡니다.
        actual_seq_len = seq_len
        if max_packets is not None:
            actual_seq_len = min(seq_len, max_packets)
            
        x = self.layer_norm(x)
        h_t = self.direction_embedding(direction_idx)
        all_outputs = []

        for t in range(actual_seq_len):
            x_t = x[:, t, :] 
            h_t = self.gru_cell(x_t, h_t)
            
            pred = self.sigmoid(self.classifier(h_t))
            all_outputs.append(pred)
            
            # 조기 종료 로직
            if enable_early_exit and batch_size == 1:
                if t >= 1:
                    current_prob = pred.item()
                    previous_prob = all_outputs[-2].item()
                    if abs(current_prob - previous_prob) < tolerance:
                        return current_prob, t + 1
        
        # 실제 추론 (batch_size == 1)
        if enable_early_exit and batch_size == 1:
            return all_outputs[-1].item(), actual_seq_len
            
        # 학습용 리턴
        return torch.stack(all_outputs, dim=1)


def get_flow_stats(filename, target_flow_size=100000):
    """
    특정 JSONL 파일에서 target_flow_size의 CDF 값을 내림하여 계산하고,
    'x' 데이터의 각 피처별 평균과 분산을 계산하여 반환합니다.
    
    Returns:
        tuple: (floored_cdf, feature_means, feature_variances)
    """
    total_flow_count = 0
    target_flow_count = 0
    
    # 통계 계산을 위한 누적 변수
    total_packet_count = 0
    feature_sums = None
    feature_sq_sums = None
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                data = json.loads(line)
                
                # 1. Flow Size CDF 계산용 업데이트
                if 'flow_size_bytes' in data:
                    size = data['flow_size_bytes']
                    total_flow_count += 1
                    
                    if size <= target_flow_size:
                        target_flow_count += 1
                
                # 2. X feature 평균 및 분산 계산용 누적 업데이트
                if 'x' in data and len(data['x']) > 0:
                    x_matrix = np.array(data['x'])
                    
                    # 2D 배열 (seq_len, num_features) 형태인지 확인
                    if len(x_matrix.shape) == 2:
                        # 최초 1회, 피처 개수에 맞춰 누적 배열 초기화
                        if feature_sums is None:
                            num_features = x_matrix.shape[1]
                            feature_sums = np.zeros(num_features, dtype=np.float64)
                            feature_sq_sums = np.zeros(num_features, dtype=np.float64)
                        
                        # 패킷(행) 개수 누적
                        total_packet_count += x_matrix.shape[0]
                        
                        # 각 피처(열)별 합과 제곱의 합을 누적
                        feature_sums += np.sum(x_matrix, axis=0)
                        feature_sq_sums += np.sum(x_matrix ** 2, axis=0)
                        
    except FileNotFoundError as e:
        print(e)
        return None, None, None
    except json.JSONDecodeError as e:
        print(e)
        return None, None, None

    if total_flow_count == 0:
        return None, None, None

    # --- 최종 연산 ---

    # 1. CDF 계산 (소수점 둘째 자리 내림)
    cdf_value = target_flow_count / total_flow_count
    floored_cdf = math.floor(cdf_value * 100) / 100.0
    
    # 2. 평균 및 분산 계산
    feature_means = None
    feature_vars = None
    
    if total_packet_count > 0:
        # 평균 = (값의 합) / 데이터 개수
        feature_means = feature_sums / total_packet_count
        
        # 분산 = (제곱의 평균) - (평균의 제곱)
        mean_of_sq = feature_sq_sums / total_packet_count
        sq_of_mean = feature_means ** 2
        feature_vars = mean_of_sq - sq_of_mean
        
        # 부동소수점 오차로 인해 발생할 수 있는 극미소한 음수 값 보정
        feature_vars = np.maximum(feature_vars, 0.0)
        
    return floored_cdf, feature_means, feature_vars