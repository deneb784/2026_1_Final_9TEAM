import torch
import torch.nn as nn

class Basic1DCNN(nn.Module):
    def __init__(self, num_features=11):
        super(Basic1DCNN, self).__init__()
        
        self.conv1 = nn.Conv1d(in_channels=num_features, out_channels=32, kernel_size=3)
        self.relu = nn.ReLU()
        self.pool = nn.MaxPool1d(kernel_size=2)
        self.flatten = nn.Flatten()
        self.fc = nn.Linear(in_features=32 * 4, out_features=1)

        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        x = x.permute(0, 2, 1) 

        x = self.conv1(x)
        x = self.relu(x)
        x = self.pool(x)

        x = self.flatten(x)
        
        # 분류
        x = self.fc(x)
        out = self.sigmoid(x) # 0 ~ 1 사이의 확률값 반환
        
        return out


class LSTM(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, num_classes):
        super(LSTM, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        self.fc = nn.Linear(hidden_size, num_classes)
    
    def forward(self, x):
        # Set initial hidden and cell states
        h0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        c0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        
        # Forward propagate LSTM
        out, _ = self.lstm(x, (h0, c0))
        
        # Decode the hidden state of the last time step
        out = self.fc(out[:, -1, :])
        return out


class Attention(nn.Module):
    def __init__(self, input_size, hidden_size, num_classes):
        super(Attention, self).__init__()
        self.hidden_size = hidden_size
        self.num_classes = num_classes
        self.lstm = nn.LSTM(input_size, hidden_size, batch_first=True)
        self.attention = nn.Linear(hidden_size, 1)
        self.fc = nn.Linear(hidden_size, num_classes)
    
    def forward(self, x):
        # Forward propagate LSTM
        out, _ = self.lstm(x)
        
        # Compute attention scores
        attention_scores = self.attention(out)
        attention_weights = torch.softmax(attention_scores, dim=1)
        
        # Apply attention weights to get context vector
        context_vector = torch.sum(attention_weights * out, dim=1)
        
        # Decode the context vector
        out = self.fc(context_vector)
        return out

