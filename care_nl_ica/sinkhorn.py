import torch
from torch import nn as nn


class SinkhornOperator(object):
    """
    From http://arxiv.org/abs/1802.08665
    """
    def __init__(self, num_steps: int):

        if num_steps < 1:
            raise ValueError(f"{num_steps=} should be at least 1")

        self.num_steps = num_steps

    def __call__(self, matrix: torch.Tensor) -> torch.Tensor:

        def _normalize_row(matrix: torch.Tensor) -> torch.Tensor:
            return matrix - torch.logsumexp(matrix, 1, keepdim=True)

        def _normalize_column(matrix: torch.Tensor) -> torch.Tensor:
            return matrix - torch.logsumexp(matrix, 0, keepdim=True)

        S = matrix

        for _ in range(self.num_steps):
            S = _normalize_column(_normalize_row(S))

        return torch.exp(S)


class SinkhornNet(nn.Module):
    def __init__(self, num_dim: int, num_steps: int, temperature: float = 1):
        super().__init__()

        self.num_dim = num_dim
        self.temperature = temperature

        self.sinkhorn_operator = SinkhornOperator(num_steps)
        # self.weight = nn.Parameter(nn.Linear(num_dim, num_dim).weight+0.5*torch.ones(num_dim,num_dim), requires_grad=True)
        self.weight = nn.Parameter(torch.ones(num_dim, num_dim), requires_grad=True)

    @property
    def doubly_stochastic_matrix(self) -> torch.Tensor:
        return self.sinkhorn_operator(self.weight / self.temperature)

    def forward(self, x) -> torch.Tensor:
        if (dim_idx:=x.shape.index(self.num_dim)) == 0 or len(x.shape)==3:
            return self.doubly_stochastic_matrix @ x
        elif dim_idx == 1:
            return x @ self.doubly_stochastic_matrix

    def to(self, device):
        """
        Move the model to the specified device.

        :param device: The device to move the model to.
        """
        super().to(device)
        self.weight = self.weight.to(device)

        return self
