import torch
import torch.nn as nn

class DynamicPacketGRU(nn.Module):
    def __init__(self, input_size=11, hidden_size=64):
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

    def forward(self, x, direction_idx, enable_early_exit=False, tolerance=0.05):
        """
        x: 패킷 시퀀스 (batch_size, seq_len, input_size) 
           -> seq_len은 3, 5, 10 등 동적으로 변할 수 있음
        """
        batch_size, seq_len, _ = x.size()
        
        # 입력 데이터 정규화 (패킷 크기, 시간 간격 등의 스케일을 맞춤)
        x = self.layer_norm(x)
        
        # 방향 임베딩을 이용해 초기 은닉 상태 h_0 생성 (batch_size, hidden_size)
        h_t = self.direction_embedding(direction_idx)
        
        all_outputs = []

        for t in range(seq_len):
            x_t = x[:, t, :] # 현재 시점의 실제 패킷 정보
            
            # GRU 연산: 현재 입력(x_t)과 과거 정보(h_t) 결합
            h_t = self.gru_cell(x_t, h_t)
            
            # 현재 시점의 예측값 도출
            pred = self.sigmoid(self.classifier(h_t))
            all_outputs.append(pred)
            
            # 조기 종료 로직 (Inference 시, batch_size=1 일 때만 동작하도록 안전장치 추가)
            if enable_early_exit and batch_size == 1:
                if t >= 1:
                    current_prob = pred.item()
                    previous_prob = all_outputs[-2].item()

                    if abs(current_prob - previous_prob) < tolerance:
                        return current_prob, t + 1
        
        # 실제 추론
        if enable_early_exit and batch_size == 1:
            return all_outputs[-1].item(), seq_len
            
        # 학습용
        return torch.stack(all_outputs, dim=1)

        