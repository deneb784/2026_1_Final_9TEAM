import torch
import torch.nn as nn

class Basic1DCNN(nn.Module):
    def __init__(self, num_features=11):
        super(Basic1DCNN, self).__init__()
        
        # 1. 합성곱 층 (Convolutional Layer)
        # in_channels: 입력 피처의 개수 (11)
        # out_channels: 필터(특징 맵)의 개수 (사용자 지정, 여기서는 32개)
        # kernel_size: 한 번에 볼 시간 스텝의 크기 (여기서는 3)
        self.conv1 = nn.Conv1d(in_channels=num_features, out_channels=32, kernel_size=3)
        self.relu = nn.ReLU()
        
        # 2. 풀링 층 (Pooling Layer)
        # 데이터의 공간적 크기를 줄여 연산량을 감소시키고 중요한 특징을 추출
        self.pool = nn.MaxPool1d(kernel_size=2)
        
        # 3. 완전 연결 층 (Fully Connected Layer)
        self.flatten = nn.Flatten()
        
        # 차원 계산:
        # 시간 스텝 10 -> Conv1d(kernel=3) 통과 후 8 (10 - 3 + 1)
        # -> MaxPool1d(kernel=2) 통과 후 4 (8 / 2)
        # 최종적으로 32개의 채널이 길이 4를 가지므로 32 * 4 = 128
        self.fc = nn.Linear(in_features=32 * 4, out_features=1)
        
        # 4. 출력 층 (이진 분류용)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # 입력 x의 형태: (Batch Size, 10, 11)
        
        # PyTorch Conv1d 처리를 위해 형태 변환: (Batch Size, 11, 10)
        x = x.permute(0, 2, 1) 
        
        # 특징 추출
        x = self.conv1(x)
        x = self.relu(x)
        x = self.pool(x)
        
        # 1차원으로 펼치기
        x = self.flatten(x)
        
        # 분류
        x = self.fc(x)
        out = self.sigmoid(x) # 0 ~ 1 사이의 확률값 반환
        
        return out

