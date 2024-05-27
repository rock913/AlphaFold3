import torch
import torch.nn as nn
from typing import Optional, Tuple, Sequence, Union

from src.models.components.primitives import Linear, LayerNorm
from src.models.components.backbone_update import BackboneUpdate
from src.models.components.invariant_point_attention import InvariantPointAttention
from src.models.components.structure_transition import StructureTransition

from src.utils.geometry.rigid_matrix_vector import Rigid3Array
from src.common import residue_constants
from src.common.residue_constants import restype_atom14_rigid_group_positions
from src.utils.tensor_utils import dict_multimap


class StructureModule(nn.Module):
    def __init__(
            self,
            c_s: int,
            c_z: int,
            c_ipa: int,
            no_heads_ipa: int,
            no_qk_points: int,
            no_v_points: int,
            dropout_rate: float,
            no_blocks,
            no_transition_layers: int,
            epsilon=1e-6,
            inf=1e8,
            **kwargs,
    ):
        """
        Args:
            c_s:
                Single representation channel dimension
            c_z:
                Pair representation channel dimension
            c_ipa:
                IPA hidden channel dimension
            no_heads_ipa:
                Number of IPA heads
            no_qk_points:
                Number of query/key points to generate during IPA
            no_v_points:
                Number of value points to generate during IPA
            dropout_rate:
                Dropout rate used throughout the layer
            no_blocks:
                Number of structure module blocks
            epsilon:
                Small number used for numerical stability
            inf:
                Large number used for attention masking
        """
        super(StructureModule, self).__init__()

        self.c_s = c_s
        self.c_z = c_z
        self.c_ipa = c_ipa
        self.no_heads_ipa = no_heads_ipa
        self.no_qk_points = no_qk_points
        self.no_v_points = no_v_points
        self.dropout_rate = dropout_rate
        self.no_blocks = no_blocks
        self.no_transition_layers = no_transition_layers
        self.epsilon = epsilon
        self.inf = inf

        # Buffers to be lazily initialized later
        # self.lit_positions

        self.layer_norm_s = LayerNorm(self.c_s)
        self.layer_norm_z = LayerNorm(self.c_z)

        self.linear_in = Linear(self.c_s, self.c_s)

        self.ipa = InvariantPointAttention(
            self.c_s,
            self.c_z,
            self.c_ipa,
            self.no_heads_ipa,
            self.no_qk_points,
            self.no_v_points,
            inf=self.inf,
            eps=self.epsilon,
        )

        self.ipa_dropout = nn.Dropout(self.dropout_rate)
        self.layer_norm_ipa = LayerNorm(self.c_s)

        self.transition = StructureTransition(
            self.c_s,
            self.no_transition_layers,
            self.dropout_rate,
        )

        self.bb_update = BackboneUpdate(self.c_s, full_quat=False)

    def _forward_multimer(
            self,
            evoformer_output_dict,
            mask=None,
    ):
        s = evoformer_output_dict["single"]  # the single representation

        if mask is None:
            # [*, N]
            mask = s.new_ones(s.shape[:-1])

        # [*, N, C_s]
        s = self.layer_norm_s(s)

        # [*, N, N, C_z]
        z = self.layer_norm_z(evoformer_output_dict["pair"])

        # [*, N, C_s]
        s = self.linear_in(s)

        # [*, N]
        rigids = Rigid3Array.identity(
            s.shape[:-1],
            s.device,
        )
        outputs = []
        for i in range(self.no_blocks):
            # [*, N, C_s]
            s = s + self.ipa(s, z, rigids, mask)
            s = self.ipa_dropout(s)
            s = self.layer_norm_ipa(s)
            s = self.transition(s)

            # [*, N]
            rigids = rigids @ self.bb_update(s)  # compose

            # Convert to atom positions
            pred_xyz = self.frames_to_atom4_pos(rigids)

            preds = {
                "frames": rigids.to_tensor(),
                "positions": pred_xyz,
            }

            preds = {k: v.to(dtype=s.dtype) for k, v in preds.items()}  # dtype conversion

            outputs.append(preds)

            if i + 1 < self.no_blocks:  # if l < N_layer
                # Stop rotation gradient in between iterations to stabilize training
                rigids = rigids.stop_rot_gradient()

        del z
        # Stack all the intermediate and final outputs along the first dimension
        outputs = dict_multimap(torch.stack, outputs)
        outputs["single"] = s  # add single representation

        return outputs

    def forward(
            self,
            evoformer_output_dict,
            mask=None,
    ):
        """
        Args:
            evoformer_output_dict:
                Dictionary containing:
                    "single":
                        [*, N_res, C_s] single representation
                    "pair":
                        [*, N_res, N_res, C_z] pair representation
            mask:
                Optional [*, N_res] sequence mask
        Returns:
            A dictionary of outputs containing:
                "single":
                    [*, N_res, C_s] the single representation
                "frames":
                    backbone frames of shape [no_blocks, *, N_res]
                "positions":
                    xyz positions of shape [no_blocks, *, N_res, 4, 3]
        """
        outputs = self._forward_multimer(evoformer_output_dict, mask)
        return outputs

    def _init_residue_constants(self, float_dtype, device):
        """Initialize the literature positions on the correct device."""
        if not hasattr(self, "lit_positions"):
            self.register_buffer(
                "lit_positions",
                torch.tensor(
                    restype_atom14_rigid_group_positions,
                    dtype=float_dtype,
                    device=device,
                    requires_grad=False,
                ),
                persistent=False,
            )

    def frames_to_atom4_pos(
            self,
            frames: Rigid3Array,  # [*, N_res]
            reference_atom: str = "ALA",
    ):
        """Given backbone frames, convert to atom positions using the literature positions
        of the reference atom.
        Args:
            frames: the backbone frames
            reference_atom: the atom name to use as the reference in 3-letter code
        """
        # Lazily initialize the residue constants on the correct device
        self._init_residue_constants(frames.dtype, frames.device)

        # Extract the reference atom position
        one_letter_ref_atom = residue_constants.restype_3to1[reference_atom]
        order = residue_constants.restype_order[one_letter_ref_atom]
        backbone_atoms = self.lit_positions[order][:4]  # [4, 3]

        # Apply rigid transformations to the reference atom
        frames = frames.unsqueeze(dim=-1)  # [*, N_res, 1]
        backbone_xyz = frames.apply(backbone_atoms)  # [*, N, 4, 3]
        return backbone_xyz
