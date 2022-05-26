from typing import List

import torch
import torch.nn as nn

import care_nl_ica.cl_ica.layers as ls
from care_nl_ica.models.sinkhorn import SinkhornNet
from care_nl_ica.models.sparsity import SparseBudgetNet

FeatureList = List[int]


class ARMLP(nn.Module):
    def __init__(
        self,
        num_vars: int,
        transform: callable = None,
        residual: bool = False,
        num_weights: int = 5,
        triangular=True,
        budget: bool = False,
        weight_init_fn=None,
        gain=1.0,
    ):
        super().__init__()

        self.num_vars = num_vars
        self.residual = residual
        self.triangular = triangular
        self.budget = budget
        self.num_weights = num_weights

        if weight_init_fn is None:
            self.weight_init_fn = lambda x, gain: x * gain
        elif weight_init_fn == "orthogonal":
            self.weight_init_fn = nn.init.orthogonal_
        elif weight_init_fn == "xavier_normal":
            self.weight_init_fn = nn.init.xavier_normal_
        elif weight_init_fn == "xavier_uniform":
            self.weight_init_fn = nn.init.xavier_uniform_
        elif weight_init_fn == "sparse":
            self.weight_init_fn = nn.init.sparse_

        self.gain = gain

        if self.budget is True:
            self.budget_net = SparseBudgetNet(self.num_vars)

        self.weight = nn.ParameterList(
            [
                nn.Parameter(
                    torch.tril(
                        self.weight_init_fn(
                            nn.Linear(num_vars, num_vars).weight, self.gain
                        ),
                        0 if self.residual is False else -1,
                    )
                    if self.triangular is True
                    else self.weight_init_fn(
                        nn.Linear(num_vars, num_vars, bias=False).weight, self.gain
                    )
                )
                for _ in range(self.num_weights)
            ]
        )
        if self.residual is True and self.triangular is True:
            self.scaling = nn.Parameter(torch.ones(self.num_vars), requires_grad=True)

        # structure injection
        self.transform = transform if transform is not None else lambda w: w
        self.permutation = lambda x: self.permutation_matrix @ x
        self.permutation_matrix = torch.eye(self.num_vars)
        self.struct_mask = torch.ones_like(self.weight[0], requires_grad=False)

    @property
    def assembled_weight(self):
        w = torch.ones_like(self.weight[0])
        for i in range(len(self.weight)):
            w *= self.weight[i]

        w = (
            w
            if (self.residual is False or self.triangular is False)
            else w + torch.diag(self.scaling)
        )

        assembled = w if self.triangular is False else torch.tril(w)

        if self.budget is True:
            assembled = assembled * self.budget_net.mask
        return assembled

    def make_triangular_with_permute(
        self, tri_weight: torch.Tensor, permute: torch.Tensor
    ):

        self.triangular = True
        self.residual = False
        self.transform = lambda x: x

        print(
            "--------Setting bottleneck weights, switching to triangular structure with no residuality and no transform--------"
        )

        self.assembled_weight = tri_weight
        self.permutation_matrix = permute

    @assembled_weight.setter
    def assembled_weight(self, value):

        self.weight = nn.ParameterList(
            [
                nn.Parameter(value, requires_grad=True),
                *[
                    nn.Parameter(torch.tril(torch.ones_like(value)), requires_grad=True)
                    for _ in range(self.num_weights - 1)
                ],
            ]
        )

    def forward(self, x):
        return self.transform(self.assembled_weight) @ self.permutation(x)

    def to(self, device):
        """
        Move the model to the specified device.

        :param device: The device to move the model to.
        """
        super().to(device)
        self.weight = self.weight.to(device)
        self.permutation_matrix = self.permutation_matrix.to(device)

        if self.residual is True:
            self.scaling = self.scaling.to(device)

        if self.budget is True:
            self.budget_net = self.budget_net.to(device)
        return self

    def inject_structure(self, adj_mat, inject_structure=False):
        if inject_structure is True:
            # set structural mask
            self.struct_mask = (adj_mat.abs() > 0).float()
            self.struct_mask.requires_grad = False

            # set transform to include structural mask
            self.transform = lambda w: self.struct_mask * w

            print(f"Injected structure with weight: \n {self.struct_mask}")


class FeatureMLP(nn.Module):
    def __init__(
        self,
        num_vars: int,
        in_features: int,
        out_feature: int,
        bias: bool = True,
        force_identity: bool = False,
    ):
        super().__init__()
        self.num_vars = num_vars
        self.in_features = in_features
        self.out_feature = out_feature
        self.bias = bias

        # create MLPs
        self.mlps = nn.ModuleList(
            [
                nn.Linear(self.in_features, self.out_feature, self.bias)
                for _ in range(self.num_vars)
            ]
        )

        self.act = nn.ModuleList(
            [nn.LeakyReLU(negative_slope=0.25) for _ in range(self.num_vars)]
        )
        if force_identity is True:
            self.act = nn.ModuleList([nn.Identity() for _ in range(self.num_vars)])
            print("-----------------using identity activation-----------------")

    def forward(self, x):
        """

        :param x: tensor of size (batch x num_vars x in_features)
        :return:
        """

        if self.in_features == 1 and len(x.shape) == 2:
            x = torch.unsqueeze(x, 2)

        # the ith layer only gets the ith variable
        # reassemble into shape (batch_size, num_vars, out_features)
        return torch.stack(
            [self.act[i](mlp(x[:, i, :])) for i, mlp in enumerate(self.mlps)], dim=1
        )

    def to(self, device):
        """
        Move the model to the specified device.

        :param device: The device to move the model to.
        """
        super().to(device)
        self.mlps = self.mlps.to(device)
        self.act = self.act.to(device)
        return self


class ARBottleneckNet(nn.Module):
    def __init__(
        self,
        num_vars: int,
        pre_layer_feats: FeatureList,
        post_layer_feats: FeatureList,
        bias: bool = True,
        residual: bool = False,
        sinkhorn=False,
        triangular=True,
        budget: bool = False,
        weight_init_fn=None,
        gain=1.0,
    ):
        super().__init__()
        self.num_vars = num_vars
        self.pre_layer_feats = pre_layer_feats
        self.post_layer_feats = post_layer_feats
        self.bias = bias

        self._init_feature_layers()

        self.ar_bottleneck = ARMLP(
            self.num_vars,
            residual=residual,
            triangular=triangular,
            budget=budget,
            weight_init_fn=weight_init_fn,
            gain=gain,
        )

        self.sinkhorn = SinkhornNet(self.num_vars, 5, 1e-3)

        self.inv_permutation = torch.arange(self.num_vars)

        self.permutation = (
            (lambda x: x) if sinkhorn is False else (lambda x: self.sinkhorn(x))
        )

    def _layer_generator(self, features: FeatureList):
        return nn.Sequential(
            *[
                FeatureMLP(self.num_vars, features[idx], features[idx + 1], self.bias)
                for idx in range(len(features) - 1)
            ]
        )

    def _init_feature_layers(self):
        """
        Initialzies the feature transform layers before and after the bottleneck.

        :return:
        """
        # check argument validity
        if not len(self.pre_layer_feats) and not len(self.post_layer_feats):
            raise ValueError(f"No pre- and post-layer specified!")

        self._init_pre_layers()
        self._init_post_layers()

    def _init_pre_layers(self):
        """
        Initialzies the feature transform layers before the bottleneck.

        :return:
        """
        if len(self.pre_layer_feats):
            # check feature values at the "interface"
            # input (coming from the outer world) has num_features=1
            if (first_feat := self.pre_layer_feats[0]) != 1:
                raise ValueError(f"First feature size should be 1, got {first_feat}!")

            # create layers with ReLU activations
            self.pre_layers = self._layer_generator(self.pre_layer_feats)

    def _init_post_layers(self):
        """
        Initialzies the feature transform layers after the bottleneck.

        :return:
        """
        if len(self.post_layer_feats):

            # check feature values at the "interface"
            # output has num_features=1
            if (last_feat := self.post_layer_feats[-1]) != 1:
                raise ValueError(f"Last feature size should be 1, got {last_feat}!")

            # create layers with ReLU activations
            self.post_layers = self._layer_generator(self.post_layer_feats)

    def forward(self, x):
        return torch.squeeze(
            self.post_layers(self.ar_bottleneck(self.permutation(self.pre_layers(x))))
        )

        # return self.ar_bottleneck(x.T).T

    def to(self, device):
        """
        Moves the model to the specified device.

        :param device: device to move the model to
        :return: self
        """
        super().to(device)
        print(f"Moving model to {device}")
        # move the model to the specified device
        self.pre_layers = self.pre_layers.to(device)
        self.post_layers = self.post_layers.to(device)
        self.ar_bottleneck = self.ar_bottleneck.to(device)
        self.sinkhorn = self.sinkhorn.to(device)
        self.inv_permutation = self.inv_permutation.to(device)

        return self

    def bottleneck_l1_norm(self):
        return self.ar_bottleneck.assembled_weight.abs().mean()
