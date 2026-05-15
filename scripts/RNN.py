"""
RNN-based NAS Controller for CNN architecture search.

Design principles (based on literature and practice):
- Lightweight LSTM (suitable for small discrete search spaces)
- Step-wise categorical decisions
- REINFORCE-compatible interface
- Entropy regularization for exploration

Default hyperparameters are chosen for:
- Stability
- Low computational overhead
- Small search space (as in embedded/ASIC scenarios)
"""

import torch
import torch.nn as nn
from torch.distributions import Categorical


class NASController(nn.Module):
    """
    LSTM-based controller that generates architecture decisions sequentially.

    Each step corresponds to selecting one hyperparameter from a discrete set.
    """

    def __init__(
        self,
        search_space,
        hidden_size=64,
        embedding_dim=32,
        num_layers=1,
        entropy_coeff=0.01,
    ):
        """
        Args:
            search_space (list[list[int]]):
                List of candidate values per step.
                Example:
                    [
                        [1, 2, 8, 16],   # kernel width
                        [4, 8, 16],      # filters
                        [1, 4, 8, 16],   # pooling
                        ...
                    ]

            hidden_size (int): LSTM hidden dimension (default=64)
            embedding_dim (int): embedding size (default=32)
            num_layers (int): number of LSTM layers (default=1)
            entropy_coeff (float): entropy regularization weight
        """
        super().__init__()

        self.search_space = search_space
        self.num_steps = len(search_space)
        self.entropy_coeff = entropy_coeff

        # Build token dictionary (for embedding previous actions)
        self.token_to_id = {}
        self.id_to_token = []

        for choices in search_space:
            for val in choices:
                if val not in self.token_to_id:
                    self.token_to_id[val] = len(self.id_to_token)
                    self.id_to_token.append(val)

        # Add start token
        self.start_token_id = len(self.id_to_token)
        self.num_tokens = self.start_token_id + 1

        # Embedding layer
        self.embedding = nn.Embedding(self.num_tokens, embedding_dim)

        # LSTM controller
        self.lstm = nn.LSTM(
            input_size=embedding_dim,
            hidden_size=hidden_size,
            num_layers=num_layers,
        )

        # One classifier head per decision step
        self.heads = nn.ModuleList([
            nn.Linear(hidden_size, len(choices))
            for choices in search_space
        ])

        self._init_parameters()

    def _init_parameters(self):
        """Initialize parameters for stable training."""
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def sample(self):
        """
        Sample one architecture.

        Returns:
            architecture (list[int])
            log_prob (Tensor)
            entropy (Tensor)
        """

        device = next(self.parameters()).device

        # Initialize hidden state
        h = torch.zeros(self.lstm.num_layers, 1, self.lstm.hidden_size, device=device)
        c = torch.zeros(self.lstm.num_layers, 1, self.lstm.hidden_size, device=device)

        # Start token
        input_token = torch.tensor([self.start_token_id], device=device)

        log_prob_total = 0.0
        entropy_total = 0.0
        architecture = []

        for step in range(self.num_steps):
            # Embed previous action
            emb = self.embedding(input_token).unsqueeze(0)  # (1, 1, emb)

            # LSTM forward
            output, (h, c) = self.lstm(emb, (h, c))
            output = output.squeeze(0)  # (1, hidden)

            # Compute logits
            logits = self.heads[step](output)

            # Sample from categorical distribution
            dist = Categorical(logits=logits)
            action_idx = dist.sample()

            log_prob_total += dist.log_prob(action_idx)
            entropy_total += dist.entropy()

            # Map to actual value
            value = self.search_space[step][action_idx.item()]
            architecture.append(value)

            # Prepare next input
            token_id = self.token_to_id[value]
            input_token = torch.tensor([token_id], device=device)

        return architecture, log_prob_total, entropy_total

    def sample_batch(self, batch_size=8):
        """
        Sample multiple architectures.

        Args:
            batch_size (int): number of samples

        Returns:
            architectures (list[list[int]])
            log_probs (Tensor)
            entropies (Tensor)
        """

        archs, log_probs, entropies = [], [], []

        for _ in range(batch_size):
            arch, log_p, ent = self.sample()
            archs.append(arch)
            log_probs.append(log_p)
            entropies.append(ent)

        return (
            archs,
            torch.stack(log_probs),
            torch.stack(entropies),
        )

    def compute_loss(self, log_probs, rewards, baseline, entropies=None):
        """
        Compute REINFORCE loss with optional entropy regularization.

        Args:
            log_probs (Tensor): shape (B,)
            rewards (Tensor): shape (B,)
            baseline (float or Tensor)
            entropies (Tensor, optional): shape (B,)

        Returns:
            loss (Tensor)
        """

        advantage = rewards - baseline
        policy_loss = -(log_probs * advantage.detach()).mean()

        if entropies is None:
            return policy_loss

        return policy_loss - self.entropy_coeff * entropies.mean()
