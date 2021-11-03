from collections import Counter

import numpy as np
import torch
import wandb

from .cl_ica import latent_spaces
from .indep_check import IndependenceChecker
from .prob_utils import calc_disentanglement_scores, sample_marginal_and_conditional


class Logger(object):

    def __init__(self, hparams, model) -> None:
        super().__init__()
        self.hparams = hparams

        self._setup_exp_management(model)

        self.total_loss_values = None

    def _setup_exp_management(self, model):
        if self.hparams.use_wandb is True:
            wandb.init(entity="causal-representation-learning", project=self.hparams.project, notes=self.hparams.notes,
                       config=self.hparams)
            wandb.watch(model, log_freq=self.hparams.n_log_steps, log="all")

            # define metrics
            wandb.define_metric("total_loss", summary="min")
            wandb.define_metric("lin_dis_score", summary="max")
            wandb.define_metric("perm_dis_score", summary="max")

    def init_log_lists(self):
        if (self.total_loss_values is not None and not self.hparams.resume_training) or self.total_loss_values is None:
            self.individual_losses_values = []
            self.total_loss_values = []
            self.lin_dis_scores = []
            self.perm_dis_scores = []
            self.causal_check = []

        self.global_step = len(self.total_loss_values) + 1

    def log(self, h, h_ind, dep_mat, enc_dec_jac, ind_checker: IndependenceChecker, latent_space: latent_spaces.LatentSpace, losses,
            total_loss, dep_loss, f, causality_metrics, ar_bottleneck=None, numerical_jacobian=None):

        self.individual_losses_values.append(losses)
        self.total_loss_values.append(total_loss)

        if self.global_step % self.hparams.n_log_steps == 1 or self.global_step == self.hparams.n_steps:

            z_disentanglement = latent_space.sample_marginal(self.hparams.n_eval_samples)
            hz_disentanglement = h(z_disentanglement)

            lin_dis_score, perm_dis_score = calc_disentanglement_scores(z_disentanglement, hz_disentanglement)
            self.lin_dis_scores.append(lin_dis_score)
            self.perm_dis_scores.append(perm_dis_score)

            if self.hparams.use_dep_mat:
                null_list = [False] * torch.numel(dep_mat)
                null_list[torch.argmin(dep_mat).item()] = True
                var_map = [1, 1, 2, 2]
            else:
                null_list, var_map = ind_checker.check_bivariate_dependence(h_ind(z_disentanglement),
                                                                            hz_disentanglement)

            ######Note this is specific to a dense 2x2 triangular matrix!######
            if Counter(null_list) == Counter([False, False, False, True]):
                self.causal_check.append(1.)
                print('concluded a causal effect')

                for i, null in enumerate(null_list):
                    if null:
                        print('cause variable is X{}'.format(str(var_map[i])))
            else:
                self.causal_check.append(0.)
            """
            from matplotlib import pyplot as plt
            fig_z, ax_z = plt.subplots()
            ax_z.hist(z_disentanglement.detach().cpu().numpy().flatten(), bins=100)
            writer.add_figure("hist_z", fig_z, self.global_step)
            fig_hz, ax_hz = plt.subplots()
            ax_hz.hist(hz_disentanglement.detach().cpu().numpy().flatten(), bins=100)
            writer.add_figure("hist_hz", fig_hz, global_step)
            writer.flush()
            """

        else:
            self.lin_dis_scores.append(self.lin_dis_scores[-1])
            self.perm_dis_scores.append(self.perm_dis_scores[-1])
            self.causal_check.append(self.causal_check[-1])

        self._log_to_wandb(dep_mat, enc_dec_jac, self.global_step, total_loss, dep_loss, causality_metrics, ar_bottleneck, numerical_jacobian)

        self.print_statistics(f, dep_mat, dep_loss)

        self.global_step += 1

    def print_statistics(self, f, dep_mat, dep_loss):
        if self.hparams.verbose is True and (self.global_step % self.hparams.n_log_steps == 1 or self.global_step == self.hparams.n_steps):
            print(
                f"Step: {self.global_step} \t",
                f"Loss: {self.total_loss_values[-1]:.4f} \t",
                f"<Loss>: {np.mean(np.array(self.total_loss_values[-self.hparams.n_log_steps:])):.4f} \t",
                f"Lin. Disentanglement: {self.lin_dis_scores[-1]:.4f} \t",
                f"Perm. Disentanglement: {self.perm_dis_scores[-1]:.4f}",
                f"Causal. Check: {self.causal_check[-1]:.4f}",
            )
            print(dep_mat.detach())
            print(f"{dep_loss.item()=:.4f}")

            if self.hparams.normalization == "learnable_sphere":
                print(f"r: {f[-1].r}")

    def report_final_disentanglement_scores(self, h, latent_space):
        device = self.hparams.device
        final_linear_scores = []
        final_perm_scores = []

        with torch.no_grad():
            for i in range(self.hparams.num_eval_batches):
                data = sample_marginal_and_conditional(latent_space, self.hparams.batch_size, device)
                z1, z2_con_z1, z3 = data
                z1 = z1.to(device)
                z3 = z3.to(device)
                z2_con_z1 = z2_con_z1.to(device)
                # z3 = torch.roll(z1, 1, 0)
                z1_rec = h(z1)
                z2_con_z1_rec = h(z2_con_z1)
                z3_rec = h(z3)

                linear_disentanglement_score, permutation_disentanglement_score = calc_disentanglement_scores(z1,
                                                                                                              z1_rec)
                final_linear_scores.append(linear_disentanglement_score)
                final_perm_scores.append(permutation_disentanglement_score)

        print("linear mean: {} std: {}".format(np.mean(final_linear_scores), np.std(final_linear_scores)))
        print("perm mean: {} std: {}".format(np.mean(final_perm_scores), np.std(final_perm_scores)))

    def _log_to_wandb(self, dep_mat, enc_dec_jac, global_step, total_loss, dep_loss, causality_metrics, ar_bottleneck=None, numerical_jacobian=None):
        if self.hparams.use_wandb:


            wandb.log({"total_loss": total_loss, "dep_loss" : dep_loss, "lin_dis_score": self.lin_dis_scores[-1],
                       "perm_dis_score": self.perm_dis_scores[-1]}, step=global_step)

            # wandb.log(causality_metrics, step=global_step)

            def log_matrix(name, matrix):
                labels = [f"{name}_{i}{j}" for i in range(matrix.shape[0]) for j in range(matrix.shape[1])]
                data = matrix.detach().cpu().reshape(-1, ).tolist()
                wandb.log({key: val for key, val in zip(labels, data)}, step=global_step)
            
            # log the Jacobian
            log_matrix("a", dep_mat)

            # log the Encoder-Decoder Jacobian
            log_matrix("j", enc_dec_jac)
        
            # log the numerical Jacobian
            if numerical_jacobian is not None:
                log_matrix("a_num", numerical_jacobian)
                    
            # log the bottleneck weights
            if ar_bottleneck is not None:
                log_matrix("w", ar_bottleneck)
                

    def log_summary(self, **kwargs):
        """
        Logs fixed values to the summary board

        :param kwargs: dict of values to log
        """
        if self.hparams.use_wandb is True:

            for key, value in kwargs.items():
                wandb.run.summary[key] = value
            
    def log_scatter_latent_rec(self, latent, rec, name:str):
        if self.hparams.use_wandb is True and self.hparams.log_latent_rec is True:
            if self.global_step % (20*self.hparams.n_log_steps) == 1:
                for i in range(self.hparams.n):
                    # from pdb import set_trace; set_trace()
                    table = wandb.Table(data=torch.stack((latent[:, i], rec[:, i])).T.tolist(), columns = ["latent", "rec"])

                    wandb.log({f"latent_rec_{name}_dim_{i}" : wandb.plot.scatter(table, "latent", "rec",title=f"Latents vs reconstruction of {name} in dimension {i}")})
    
