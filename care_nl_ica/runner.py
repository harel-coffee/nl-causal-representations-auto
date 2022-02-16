import torch
from torch.nn import functional as F

from care_nl_ica.dep_mat import dep_mat_metrics
from care_nl_ica.graph_utils import indirect_causes, causal_orderings
from care_nl_ica.independence.indep_check import IndependenceChecker
from care_nl_ica.logger import Logger
from care_nl_ica.models.model import ContrastiveLearningModel
from care_nl_ica.prob_utils import sample_marginal_and_conditional
from care_nl_ica.utils import unpack_item_list, save_state_dict
from cl_ica import latent_spaces
from dep_mat import calc_jacobian_loss
from care_nl_ica.metrics.metric_logger import MetricLogger
from care_nl_ica.metrics.metrics import frobenius_diagonality, corr_matrix, \
    extract_permutation_from_jacobian, permutation_loss
from prob_utils import setup_marginal, setup_conditional


class Runner(object):

    def __init__(self, hparams) -> None:
        super().__init__()

        self.hparams = hparams

        self.indep_checker = IndependenceChecker(self.hparams)
        self.model = ContrastiveLearningModel(self.hparams)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.hparams.lr)
        self.logger = Logger(self.hparams, self.model)
        self.metrics = MetricLogger()
        self.latent_space = latent_spaces.LatentSpace(space=(self.model.space),
                                                      sample_marginal=(setup_marginal(self.hparams)),
                                                      sample_conditional=(setup_conditional(self.hparams)), )

        self._calc_dep_mat()
        self._inject_encoder_structure()

        self.dep_loss = None
        self.dep_mat = None

        self.orderings = causal_orderings(self.gt_jacobian_encoder)
        print(f'{self.orderings=}')

        if self.hparams.use_wandb is True:
            self.logger.log_summary(**{"causal_orderings": self.orderings})

    def _calc_dep_mat(self) -> None:
        dep_mat = self.indep_checker.check_independence_z_gz(self.model.decoder, self.latent_space)
        # save the ground truth jacobian of the decoder
        if dep_mat is not None:

            # save the decoder jacobian including the permutation
            self.gt_jacobian_decoder_permuted = dep_mat.detach()
            if self.hparams.permute is True:
                # print(f"{dep_mat=}")
                # set_trace()
                dep_mat = dep_mat[torch.argsort(self.model.decoder.permute_indices), :]

            self.gt_jacobian_decoder = dep_mat.detach()
            self.gt_jacobian_encoder = torch.tril(dep_mat.detach().inverse())

            print(f"{self.gt_jacobian_encoder=}")

            self.logger.log_jacobian(dep_mat)

            self.indirect_causes, self.paths = indirect_causes(self.gt_jacobian_encoder)

            if self.hparams.permute is True:
                self.logger.log_summary(**{"permute_indices": self.model.decoder.permute_indices})

    def _inject_encoder_structure(self) -> None:
        if self.hparams.inject_structure is True:
            if self.hparams.use_flows:
                self.model.encoder.confidence.inject_structure(self.gt_jacobian_encoder, self.hparams.inject_structure)

            elif self.hparams.use_ar_mlp:
                self.model.encoder.ar_bottleneck.inject_structure(self.gt_jacobian_encoder,
                                                                  self.hparams.inject_structure)

    def reset_encoder(self) -> None:
        self.model.reset_encoder()
        self.optimizer = torch.optim.Adam(self.model.encoder.parameters(), lr=self.hparams.lr)

    def train_step(self, data, h, test):
        n1, n2_con_n1, n3 = self._prepare_data(data)

        self.optimizer.zero_grad()

        n1_rec, n2_con_n1_rec, n3_rec = self._forward(h, n1, n2_con_n1, n3)

        self.logger.log_scatter_latent_rec(n1, n1_rec, "n1")

        with torch.no_grad():
            z1 = self.model.decoder(n1)
            self.logger.log_scatter_latent_rec(z1, n1_rec, "z1_n1_rec")

        losses_value, total_loss_value = self._contrastive_loss(n1, n1_rec, n2_con_n1, n2_con_n1_rec, n3, n3_rec, test)

        if not self.hparams.identity_mixing_and_solution and self.hparams.lr != 0:
            # add the learnable jacobian

            total_loss_value = self._l2_loss(total_loss_value)
            total_loss_value = self._l1_loss(total_loss_value)
            total_loss_value = self._sinkhorn_entropy_loss(total_loss_value)
            total_loss_value = self._dep_loss(total_loss_value)
            total_loss_value = self._triangularity_loss(n1, n1_rec, n2_con_n1, n2_con_n1_rec, n3, n3_rec,
                                                        total_loss_value)
            total_loss_value = self._qr_loss(total_loss_value)
            total_loss_value = self._budget_loss(total_loss_value)

            total_loss_value.backward()

            self.optimizer.step()

        return total_loss_value.item(), unpack_item_list(losses_value)

    def _forward(self, h, n1, n2_con_n1, n3):
        n1_rec = h(n1)
        n2_con_n1_rec = h(n2_con_n1)
        n3_rec = h(n3)
        # n3_rec = n1_rec[n3_shuffle_indices]
        return n1_rec, n2_con_n1_rec, n3_rec

    def _contrastive_loss(self, n1, n1_rec, n2_con_n1, n2_con_n1_rec, n3, n3_rec, test):
        if test:
            total_loss_value = F.mse_loss(n1_rec, n1)
            losses_value = [total_loss_value]
        else:
            total_loss_value, _, losses_value = self.model.loss(
                n1, n2_con_n1, n3, n1_rec, n2_con_n1_rec, n3_rec
            )

            # writer.add_scalar("loss_hn", total_loss_value, global_step)
            # writer.add_scalar("loss_n", loss(
            #    n1, n2_con_n1, n3, n1, n2_con_n1, n3
            # )[0], global_step)
            # writer.flush()
        return losses_value, total_loss_value

    def _prepare_data(self, data):
        n1, n2_con_n1, n3 = data
        n1 = n1.to(self.hparams.device)
        n2_con_n1 = n2_con_n1.to(self.hparams.device)
        n3 = n3.to(self.hparams.device)
        # create random "negative" pairs
        # this is faster than sampling n3 again from the marginal distribution
        # and should also yield samples as if they were sampled from the marginal
        # import pdb; pdb.set_trace()
        # n3_shuffle_indices = torch.randperm(len(n1))
        # n3_shuffle_indices = torch.roll(torch.arange(len(n1)), 1)
        # n3 = n1[n3_shuffle_indices]
        # n3 = n3.to(device)
        return n1, n2_con_n1, n3

    def _qr_loss(self, total_loss_value, matrix_exp=False):
        if self.hparams.qr_loss != 0.0 and (self.hparams.start_step is None or (
                self.hparams.start_step is not None and self.logger.global_step >= self.hparams.start_step)):

            if self.dep_mat is not None:

                if self.hparams.use_ar_mlp is False:
                    J = self.dep_mat
                else:
                    if self.hparams.sinkhorn is False:
                        J = self.model.encoder.ar_bottleneck.assembled_weight
                    else:
                        J = self.model.encoder.ar_bottleneck.assembled_weight @ self.model.sinkhorn.doubly_stochastic_matrix

                # Q is incentivized to be the permutation for the causal ordering
                Q = extract_permutation_from_jacobian(J, self.hparams.cholesky_permutation is False)

                """
                The first step is to ensure that the Q in the QR decomposition of the transposed(bottleneck) is 
                **a permutation** matrix.
    
                The second step is to ensure that the permutation matrix is the identity. If we got a permutation matrix
                in the first step, then we could use Q.T to multiply the observations. 
                """

                # loss options
                if self.logger.global_step % 250 == 0:
                    print(f"{Q=}")

                total_loss_value += self.hparams.qr_loss * permutation_loss(Q, matrix_exp)

        return total_loss_value

    def _triangularity_loss(self, n1, n1_rec, n2_con_n1, n2_con_n1_rec, n3, n3_rec, total_loss_value):
        if self.hparams.triangularity_loss != 0. and (self.hparams.start_step is None or (
                self.hparams.start_step is not None and self.logger.global_step >= self.hparams.start_step)):
            # todo: these use the ground truth
            # still, they can be used to show that some supervision helps
            # pearson_n1 = corr_matrix(n1.T, n1_rec.T)
            # pearson_n2_con_n1 = corr_matrix(n2_con_n1.T, n2_con_n1_rec.T)
            # pearson_n3 = corr_matrix(n3.T, n3_rec.T)
            # total_loss_value += self.hparams.triangularity_loss*(frobenius_diagonality(pearson_n1.abs()) + frobenius_diagonality(
            #     pearson_n2_con_n1.abs()) + frobenius_diagonality(pearson_n3.abs()))

            # correlation between observation and reconstructed latents
            # exploits the assumption that the SEM has a lower-triangular Jacobian
            # order is important due to the triangularity loss
            m = self.model.sinkhorn.doubly_stochastic_matrix

            pearson_n1 = corr_matrix(self.model.decoder(n1).T, n1_rec.T)
            pearson_n2_con_n1 = corr_matrix(self.model.decoder(n2_con_n1).T, n2_con_n1_rec.T)
            pearson_n3 = corr_matrix(self.model.decoder(n3).T, n3_rec.T)
            # total_loss_value += self.hparams.triangularity_loss * (triangularity_loss(pearson_n1) + triangularity_loss(pearson_n2_con_n1) + triangularity_loss(pearson_n3)) 

            total_loss_value += self.hparams.triangularity_loss * (
                    frobenius_diagonality(pearson_n1.abs()) + frobenius_diagonality(
                pearson_n2_con_n1.abs()) + frobenius_diagonality(pearson_n3.abs()))

            """
            Problems: both Sinkhorn and QR degenerates to the identity
            
            What we could do: 
                1. calculate the correlation matrice above
                2. DETACH
                3. deploy Sinkhorn to minimize the triangularity loss
            -> this would separate ICA + learning the ordering
            
            
            """

        return total_loss_value

    def _dep_loss(self, total_loss_value):
        if self.dep_loss is not None:
            total_loss_value += self.dep_loss
        return total_loss_value

    def _sinkhorn_entropy_loss(self, total_loss_value):
        if self.hparams.entropy_coeff != 0. and self.hparams.permute is True:
            total_loss_value += self.hparams.entropy_coeff * self.model.sinkhorn_entropy

        return total_loss_value

    def _budget_loss(self, total_loss_value):
        if self.hparams.budget != 0.0 and self.hparams.use_ar_mlp is True:
            total_loss_value += self.hparams.budget * self.model.encoder.ar_bottleneck.budget_net.budget_loss

            if self.hparams.entropy_coeff != 0.:
                total_loss_value += self.hparams.entropy_coeff * self.model.encoder.ar_bottleneck.budget_net.entropy

        return total_loss_value

    def _l1_loss(self, total_loss_value):
        if self.hparams.l1 != 0 and self.hparams.use_ar_mlp is True:
            # add sparsity loss to the AR MLP bottleneck
            total_loss_value += self.hparams.l1 * self.model.encoder.bottleneck_l1_norm
        return total_loss_value

    def _l2_loss(self, total_loss_value):
        if self.hparams.l2 != 0.0:
            l2: float = 0.0
            for param in self.model.encoder.parameters():
                l2 += torch.sum(param ** 2)

            total_loss_value += self.hparams.l2 * l2
        return total_loss_value

    def training_loop(self):
        for learning_mode in self.hparams.learning_modes:
            print("supervised test: {}".format(learning_mode))

            self.logger.init_log_lists()

            while (self.logger.global_step <= self.hparams.n_steps if learning_mode else self.logger.global_step <= (
                    self.hparams.n_steps * self.hparams.more_unsupervised)):

                data = sample_marginal_and_conditional(self.latent_space, size=self.hparams.batch_size,
                                                       device=self.hparams.device)

                dep_loss, dep_mat, numerical_jacobian, enc_dec_jac = calc_jacobian_loss(self.model, self.latent_space)

                self.dep_loss = dep_loss

                # Update the metrics
                threshold = 3e-5
                self.dep_mat = dep_mat
                dep_mat = dep_mat.detach()
                self.metrics.update(y_pred=(dep_mat.abs() > threshold).bool().cpu().reshape(-1, 1),
                                    y_true=(self.gt_jacobian_encoder.abs() > threshold).bool().cpu().reshape(-1, 1))

                jacobian_metrics = dep_mat_metrics(dep_mat, self.gt_jacobian_encoder, self.indirect_causes,
                                                   self.gt_jacobian_decoder_permuted, threshold)

                # if self.hparams.use_flows:
                #     dep_mat = self.model.encoder.confidence.mask()

                if self.hparams.lr != 0:
                    total_loss, losses = self.train_step(data, h=self.model.h, test=learning_mode)
                else:
                    with torch.no_grad():
                        total_loss, losses = self.train_step(data, h=self.model.h, test=learning_mode)

                self.logger.log(self.model.h, self.model.h_ind, dep_mat, enc_dec_jac, self.indep_checker,
                                self.latent_space, losses, total_loss, dep_loss, self.model.encoder,
                                self.metrics.compute(),
                                None if self.hparams.use_ar_mlp is False else self.model.encoder.ar_bottleneck.assembled_weight,
                                numerical_jacobian, jacobian_metrics, None if (
                            self.hparams.permute is False or self.hparams.use_sem is False or self.hparams.sinkhorn is False) else self.model.sinkhorn.doubly_stochastic_matrix)

            save_state_dict(self.hparams, self.model.encoder, "{}_f.pth".format("sup" if learning_mode else "unsup"))
            torch.cuda.empty_cache()

            self.reset_encoder()

        self.logger.log_jacobian(dep_mat, "learned_last", log_inverse=False)
        self.logger.report_final_disentanglement_scores(self.model.h, self.latent_space)
