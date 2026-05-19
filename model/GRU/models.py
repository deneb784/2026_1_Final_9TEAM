import torch
import torch.nn as nn

class DiffEarlyExitGRU(nn.Module):
    def __init__(self, input_size=11, hidden_size=64):
        super(DiffEarlyExitGRU, self).__init__()
        self.hidden_size = hidden_size
        self.input_size = input_size
        
        # 1. 방향(direction) 임베딩 레이어
        self.direction_embedding = nn.Embedding(num_embeddings=2, embedding_dim=hidden_size)
        
        # 2. 방향 임베딩과 첫 번째 패킷(x_0)을 결합하여 초기 은닉 상태(h_0)를 만드는 레이어
        self.init_h_layer = nn.Linear(hidden_size + input_size, hidden_size)
        
        # 3. 매 time step 마다 제어하기 위해 nn.GRUCell 사용 (입력은 차이값 11차원)
        self.gru_cell = nn.GRUCell(input_size=input_size, hidden_size=hidden_size)
        
        # 4. 0~1 사이의 연속적인 추론값(수치)을 도출하기 위한 출력층
        self.classifier = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid() # 출력을 0.0 ~ 1.0 사이의 실수 영역으로 제한

    def forward(self, x, direction_idx, enable_early_exit=False, tolerance=0.01):
        """
        x: 패킷 시퀀스 데이터 (batch_size, seq_len, input_size)
        direction_idx: 방향을 나타내는 인덱스 (batch_size,)
        enable_early_exit: 추론 시 True로 설정하여 조기 종료 활성화
        tolerance: 조기 종료를 결정할 수렴 임계값 (직전 스텝과의 차이 기준)
        """
        batch_size, seq_len, _ = x.size()
        
        # 1) 첫 번째 패킷과 방향 정보로 초기값(h_0) 설정
        dir_emb = self.direction_embedding(direction_idx) # (batch_size, hidden_size)
        x_0 = x[:, 0, :] # 첫 번째 패킷 (batch_size, input_size)
        
        # h_0 생성 및 활성화 함수 적용
        h_t = torch.tanh(self.init_h_layer(torch.cat([dir_emb, x_0], dim=-1)))
        
        all_outputs = []
        
        # 첫 번째 패킷(t=0)에 대한 예측값 계산 및 저장
        pred_0 = self.sigmoid(self.classifier(h_t))
        all_outputs.append(pred_0)
        
        # 시퀀스 길이가 1인 데이터가 들어왔을 때의 예외 처리
        if seq_len == 1:
            if enable_early_exit:
                return pred_0.item(), 1
            else:
                return torch.stack(all_outputs, dim=1)
                
        # 2) t=1 부터 '이전 패킷과의 차이(Diff)'를 입력하며 순회
        for t in range(1, seq_len):
            diff_t = x[:, t, :] - x[:, t-1, :]
            
            h_t = self.gru_cell(diff_t, h_t)
            
            pred = self.sigmoid(self.classifier(h_t))
            all_outputs.append(pred)
            
            if enable_early_exit:

                if t >= 2:
                    current_prob = pred.item()
                    previous_prob = all_outputs[-2].item()
                    
                    if abs(current_prob - previous_prob) < tolerance:
                        return current_prob, t + 1
                    

        if enable_early_exit:
            return pred.item(), seq_len
        else:
            return torch.stack(all_outputs, dim=1)