from typing import Final

import torch
from torch import nn as nn


class SinkhornOperator(object):
    def __init__(self, num_steps: int):

        if num_steps < 1:
            raise ValueError(f"{num_steps=} should be at least 1")

        self.num_steps = num_steps


    def __call__(self, matrix: torch.Tensor) -> torch.Tensor:

        ones_column: Final = torch.ones(matrix.shape[0], 1)

        def _normalize_row(matrix: torch.Tensor) -> torch.Tensor:
            return matrix / (matrix @ ones_column @ ones_column.T)

        def _normalize_column(matrix: torch.Tensor) -> torch.Tensor:
            return matrix / (ones_column @ ones_column.T @ matrix)

        S = torch.exp(matrix)

        for _ in range(self.num_steps):
            S = _normalize_column(_normalize_row(S))

        return S


class SinkhornNet(nn.Module):
    def __init__(self, num_dim: int, num_steps: int, temperature: float = 1):
        super().__init__()

        self.temperature = temperature


        self.sinkhorn_operator = SinkhornOperator(num_steps)
        self.weight = nn.Parameter(nn.Linear(num_dim, num_dim).weight, requires_grad=True)

    @property
    def doubly_stochastic_matrix(self) ->torch.Tensor:
        return self.sinkhorn_operator(self.weight/self.temperature)

    def to(self, device):
        """
        Move the model to the specified device.

        :param device: The device to move the model to.
        """
        super().to(device)
        self.weight = self.weight.to(device)

        return self


class DoublyStochasticMatrix(nn.Module):
    def __init__(self, num_vars: int, temperature: float = 1.):
        super().__init__()

        self.temperature = temperature
        self.num_vars = num_vars
        self.weight = nn.Parameter(nn.Linear(num_vars - 1, num_vars - 1).weight)

    @property
    def matrix(self):
        beta = torch.sigmoid(self.weight / self.temperature)

        l = ...
        u = ...
        x = l + beta * (u - l)

        return x
