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


class StaticConditionedEarlyExitGRU(nn.Module):
    def __init__(
        self,
        input_dim,
        static_dim,
        hidden_dim=128,
        exit_threshold=0.9
    ):
        super().__init__()

        self.hidden_dim = hidden_dim
        self.exit_threshold = exit_threshold

        # =====================================
        # Static Feature Conditioning
        # =====================================

        self.static_encoder = nn.Sequential(
            nn.Linear(static_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU()
        )

        # Initial hidden state h0
        self.h0_proj = nn.Linear(hidden_dim, hidden_dim)

        # Feature-wise gate
        self.gate_proj = nn.Sequential(
            nn.Linear(hidden_dim, input_dim),
            nn.Sigmoid()
        )

        # =====================================
        # GRU Cell
        # =====================================

        self.gru_cell = nn.GRUCell(
            input_dim,
            hidden_dim
        )

        # =====================================
        # Binary Classifier
        # =====================================

        self.classifier = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )

        # =====================================
        # Early Exit Head
        # =====================================

        self.exit_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid()
        )

    def forward(self, x, static_features, lengths):
        """
        x:
            [B, T, F]

        static_features:
            [B, static_dim]

        lengths:
            [B]
        """

        device = x.device

        B, T, F = x.shape

        # =====================================
        # Static Conditioning
        # =====================================

        z = self.static_encoder(static_features)

        # Initial hidden state
        h = self.h0_proj(z)

        # Feature-wise gate
        gate = self.gate_proj(z)

        # =====================================
        # Output Buffers
        # =====================================

        final_logits = torch.zeros(B, 1, device=device)

        exit_steps = torch.zeros(
            B,
            dtype=torch.long,
            device=device
        )

        finished = torch.zeros(
            B,
            dtype=torch.bool,
            device=device
        )

        all_logits = []
        all_exit_probs = []

        # =====================================
        # Sequential Processing
        # =====================================

        for t in range(T):

            x_t = x[:, t, :]  # [B, F]

            # ---------------------------------
            # Static-conditioned gating
            # ---------------------------------

            gated_x_t = x_t * gate

            # ---------------------------------
            # GRU update
            # ---------------------------------

            h = self.gru_cell(gated_x_t, h)

            # ---------------------------------
            # Classification
            # ---------------------------------

            logits = self.classifier(h)

            # ---------------------------------
            # Early Exit Probability
            # ---------------------------------

            exit_prob = self.exit_head(h)

            all_logits.append(logits)
            all_exit_probs.append(exit_prob)

            # ---------------------------------
            # Early Exit Decision
            # ---------------------------------

            should_exit = (
                (exit_prob.squeeze(-1) > self.exit_threshold)
                & (~finished)
                & (t < lengths)
            )

            # Save outputs
            final_logits[should_exit] = logits[should_exit]

            exit_steps[should_exit] = t

            finished = finished | should_exit

        # =====================================
        # Never-exited samples
        # =====================================

        never_finished = ~finished

        if never_finished.any():

            final_logits[never_finished] = logits[never_finished]

            exit_steps[never_finished] = (
                lengths[never_finished] - 1
            )

        return {
            "logits": final_logits,
            "exit_steps": exit_steps,
            "all_logits": all_logits,
            "all_exit_probs": all_exit_probs,
            "gate": gate,
            "z": z
        }