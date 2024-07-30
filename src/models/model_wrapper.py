import hydra
import lightning as L
import rootutils
import torch
from lightning import Callback, LightningDataModule, LightningModule, Trainer
from src.utils.tensor_utils import tensor_tree_map
from src.models.model import AlphaFold3
from src.utils.validation_metrics import drmsd, lddt
from src.utils.loss import AlphaFold3Loss
from src.utils.exponential_moving_average import ExponentialMovingAverage


class AlphaFoldWrapper(LightningModule):
    def __init__(self, config):
        super(AlphaFoldWrapper, self).__init__()
        self.config = config
        self.model = AlphaFold3(config)

        self.loss = AlphaFold3Loss(config.loss)  # AlphaFold3 loss

        self.ema = ExponentialMovingAverage(model=self.model, decay=config.ema_decay)

        self.cached_weights = None
        self.last_lr_step = -1
        self.save_hyperparameters()

    def forward(self, batch, training=True):
        return self.model(batch, train=training)

    def _log(self, batch, outputs, loss_breakdown=None, train=True):
        # Loop over loss values and log it
        phase = "train" if train else "val"
        if loss_breakdown is not None:
            for loss_name, indiv_loss in loss_breakdown.items():
                self.log(
                    f"{phase}/{loss_name}",
                    indiv_loss,
                    prog_bar=(loss_name == 'loss'),
                    on_step=train, on_epoch=(not train), logger=True, sync_dist=False,
                )

        # Compute validation metrics
        other_metrics = self._compute_validation_metrics(
            batch,
            outputs,
            superimposition_metrics=(not train)
        )

        for k, v in other_metrics.items():
            self.log(
                f"{phase}/{k}",
                torch.mean(v),
                prog_bar=(k == 'loss'),
                on_step=False, on_epoch=True, logger=True, sync_dist=True,
            )

    def training_step(self, batch, batch_idx):
        # Fetch the ema to the device
        if self.ema.device != batch["aatype"].device:
            self.ema.to(batch["aatype"].device)
        batch = reshape_features(batch)  # temporary

        # Run the model
        outputs = self.forward(batch, training=True)

        # Remove the recycling dimension
        batch = tensor_tree_map(lambda t: t[..., -1], batch)

        # For multimer, multichain permutation align the batch

        # Compute loss
        loss, loss_breakdown = self.loss(
            outputs, batch, _return_breakdown=True
        )
        # Log loss and validation metrics
        self._log(
            loss_breakdown=loss_breakdown,
            batch=batch,
            outputs=outputs,
            train=True
        )
        return loss

    def on_before_zero_grad(self, *args, **kwargs):
        # Apply EMA to model
        self.ema.update(self.model)

    def validation_step(self, batch, batch_idx):
        batch = reshape_features(batch)  # temporary

        # At the start of validation, load the EMA weights
        if self.cached_weights is None:
            # model.state_dict() contains references to model weights rather
            # than copies. Therefore, we need to clone them before calling
            # load_state_dict().
            clone_param = lambda t: t.detach().clone()
            self.cached_weights = tensor_tree_map(clone_param, self.model.state_dict())
            self.model.load_state_dict(self.ema.state_dict()["params"])

        # Run the model
        outputs = self.forward(batch, training=False)
        batch = tensor_tree_map(lambda t: t[..., -1], batch)  # Remove recycling dimension

        # For multimer, multichain permutation align the batch

        # Compute and log validation metrics
        self._log(loss_breakdown=None, batch=batch, outputs=outputs, train=False)

    def on_validation_epoch_end(self):
        # Restore the model weights to normal
        self.model.load_state_dict(self.cached_weights)
        self.cached_weights = None

    def _compute_validation_metrics(
            self,
            batch,
            outputs,
            superimposition_metrics=False
    ):
        """Compute validation metrics for the model."""
        with torch.no_grad():
            batch_size, n_tokens = batch["residue_index"].shape
            metrics = {}

            gt_coords = batch["all_atom_positions"]  # (bs, n_atoms, 3)
            pred_coords = outputs["sampled_positions"]  # (bs, n_atoms, 3)
            all_atom_mask = batch["atom_mask"]  # (bs, n_atoms)

            gt_coords_masked = gt_coords * all_atom_mask[..., None]
            pred_coords_masked = pred_coords * all_atom_mask[..., None]

            # Gather representative atoms
            token_repr_atoms = batch["token_repr_atom"]  # CA atom indices (bs, n_atoms)
            batch_indices = torch.arange(batch_size).reshape(batch_size, 1)

            gt_coords_masked_ca = gt_coords_masked[batch_indices, token_repr_atoms, :]
            pred_coords_masked_ca = pred_coords_masked[batch_indices, token_repr_atoms, :]
            all_atom_mask_ca = all_atom_mask[batch_indices, token_repr_atoms]

            # TODO: fix lddt
            # lddt_ca_score = lddt(
            #    all_atom_pred_pos=pred_coords_masked_ca,
            #    all_atom_positions=gt_coords_masked_ca,
            #    all_atom_mask=all_atom_mask_ca,
            #    eps=self.config.globals.eps,
            #    per_residue=False
            # )
            # metrics["lddt_ca"] = lddt_ca_score

            # drmsd
            drmsd_ca_score = drmsd(
                pred_coords_masked_ca,
                gt_coords_masked_ca,
                mask=all_atom_mask_ca,  # still required here to compute n
            )
            metrics["drmsd_ca"] = drmsd_ca_score

            if superimposition_metrics:
                # superimpose and compute gdt_ts and gdt_ha
                pass

            return metrics

    def configure_optimizers(self):
        partial_optimizer = hydra.utils.instantiate(self.config.optimizer)
        partial_scheduler = hydra.utils.instantiate(self.config.scheduler)
        optimizer = partial_optimizer(self.trainer.model.parameters())
        scheduler = partial_scheduler(optimizer=optimizer)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "name": "AlphaFold3LRScheduler"
                # "frequency": 1,
            },
        }

    def on_load_checkpoint(self, checkpoint):
        # Load the EMA model weights
        ema = checkpoint["ema"]
        self.ema.load_state_dict(ema)

    def on_save_checkpoint(self, checkpoint):
        checkpoint["ema"] = self.ema.state_dict()

    def resume_last_lr_step(self, lr_step):
        self.last_lr_step = lr_step


def reshape_features(batch):
    """Temporary function that converts the features in the
    batch to the correct shapes for the model. Assumes only 4 backbone atoms per residue."""
    bs, n_res, _, n_cycle = batch["ref_mask"].shape
    batch["all_atom_positions"] = batch["all_atom_positions"].reshape(-1, n_res * 4, 3, n_cycle)
    batch["ref_pos"] = batch["ref_pos"].reshape(-1, n_res * 4, 3, n_cycle)
    batch["ref_mask"] = batch["ref_mask"].reshape(-1, n_res * 4, n_cycle)
    batch["ref_element"] = batch["ref_element"].reshape(-1, n_res * 4, 4, n_cycle)
    batch["ref_charge"] = batch["ref_charge"].reshape(-1, n_res * 4, n_cycle)
    batch["ref_atom_name_chars"] = batch["ref_atom_name_chars"].reshape(-1, n_res * 4, 4, n_cycle)
    batch["ref_space_uid"] = batch["ref_space_uid"].reshape(-1, n_res * 4, n_cycle)
    batch["atom_to_token"] = batch["atom_to_token"].reshape(-1, n_res * 4, n_cycle)
    batch["atom_exists"] = batch["atom_exists"].reshape(-1, n_res * 4, n_cycle)
    batch["atom_mask"] = batch["atom_mask"].reshape(-1, n_res * 4, n_cycle)
    return batch