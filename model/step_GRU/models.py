import torch
import torch.nn as nn

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