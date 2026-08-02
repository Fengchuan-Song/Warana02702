# -*- coding: utf-8 -*-
"""
Created on Sun Jul 27 11:40:27 2025

@author: Lenovo
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GATConv
from torch_geometric.utils import softmax
from .data import build_temporal_edge_index


SUPPORTED_PAIR_FEATURES = {"dx", "dy", "distance", "distance2", "theta", "speed_diff"}


def parse_temporal_scales(temporal_scales):
    if temporal_scales is None:
        return None
    if isinstance(temporal_scales, str):
        temporal_scales = [item.strip() for item in temporal_scales.split(",") if item.strip()]
    scales = sorted({int(item) for item in temporal_scales})
    if not scales or any(scale <= 0 for scale in scales):
        raise ValueError("temporal_scales must contain positive integers.")
    return scales


def parse_pair_features(pair_features):
    if pair_features is None:
        return ["dx", "dy", "distance", "distance2"]
    if isinstance(pair_features, str):
        if pair_features.strip().lower() in {"", "none", "no", "null"}:
            return []
        pair_features = [item.strip() for item in pair_features.split(",") if item.strip()]
    pair_features = list(pair_features)
    unknown = [item for item in pair_features if item not in SUPPORTED_PAIR_FEATURES]
    if unknown:
        raise ValueError(
            f"Unsupported pair feature(s): {unknown}. "
            f"Use any of: {sorted(SUPPORTED_PAIR_FEATURES)}"
        )
    return pair_features
        
class TemporalAttention(nn.Module):
    """Implements temporal attention to aggregate features across time steps."""
    def __init__(self, hidden_dim, num_heads=4):
        """
        Initialize the TemporalAttention module.

        Args:
            hidden_dim (int): Dimension of the input features.
            num_heads (int): Number of attention heads for multi-head attention (default: 4).
        """
        super().__init__()
        # Multi-head attention layer to capture temporal dependencies
        self.temporal_attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)

    def forward(self, x):
        """
        Forward pass for temporal attention.

        Args:
            x (torch.Tensor): Input tensor of shape [batch_size, seq_len, hidden_dim].

        Returns:
            torch.Tensor: Aggregated features of shape [batch_size, hidden_dim], averaged over time steps.
        """
        # Apply multi-head attention (query, key, value are the same input tensor)
        attn_out, _ = self.temporal_attn(x, x, x)
        # Add residual connection to preserve input information
        x = x + attn_out
        # Average across time dimension (seq_len) to get per-trajectory features
        return x.mean(dim=1)

class SpatialAttention(nn.Module):
    """Implements spatial attention to model interactions between trajectories."""
    def __init__(self, hidden_dim, num_heads=4):
        """
        Initialize the SpatialAttention module.

        Args:
            hidden_dim (int): Dimension of the input features.
            num_heads (int): Number of attention heads for multi-head attention (default: 4).
        """
        super().__init__()
        # Multi-head attention layer to capture spatial relationships between trajectories
        self.spatial_attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=num_heads, batch_first=True)

    def forward(self, feat_A, feat_B):
        """
        Forward pass for spatial attention.

        Args:
            feat_A (torch.Tensor): Features of dataset A, shape [N_A, hidden_dim].
            feat_B (torch.Tensor): Features of dataset B, shape [N_B, hidden_dim].

        Returns:
            torch.Tensor: Updated features for dataset A, shape [N_A, hidden_dim].
        """
        # Prepare query (Q) by adding a sequence dimension
        Q = feat_A.unsqueeze(1)  # Shape: [N_A, 1, hidden_dim]
        # Prepare key (K) and value (V) by expanding feat_B to match Q's batch size
        K = feat_B.unsqueeze(0).expand(feat_A.size(0), feat_B.size(0), feat_B.size(1))  # Shape: [N_A, N_B, hidden_dim]
        V = K  # Use same tensor for key and value
        # Apply multi-head attention to compute spatial interactions
        attn_out, _ =  self.spatial_attn(Q, K, V)  # Shape: [N_A, 1, hidden_dim]
        # Remove sequence dimension
        attn_out = attn_out.squeeze(1) + feat_A  # Shape: [N_A, hidden_dim]
        # Add residual connection to preserve input features
        return attn_out

class FeedForwardNetwork(nn.Module):
    """Implements a feed-forward network for feature transformation."""
    def __init__(self, input_dim, hidden_dim, ffn_dim=None):
        """
        Initialize the FeedForwardNetwork module.

        Args:
            input_dim (int): Input feature dimension.
            hidden_dim (int): Output feature dimension.
            ffn_dim (int, optional): Hidden layer dimension (default: hidden_dim * 2).
        """
        super().__init__()
        ffn_dim = ffn_dim or hidden_dim * 2
        # Define feed-forward network with two linear layers, ReLU, and normalization
        self.ffn = nn.Sequential(
            nn.Linear(input_dim, ffn_dim),
            nn.ReLU(),
            nn.Linear(ffn_dim, hidden_dim),
            nn.LayerNorm(hidden_dim)
        )

    def forward(self, x):
        """
        Forward pass for the feed-forward network.

        Args:
            x (torch.Tensor): Input tensor of shape [..., input_dim].

        Returns:
            torch.Tensor: Transformed tensor of shape [..., hidden_dim].
        """
        return self.ffn(x)


class UncertaintySimilarity(nn.Module):
    """Implements uncertainty-aware similarity computation with confidence adjustment."""
    
    def __init__(self, hidden_dim, input_dim, epsilon=1e-6, temperature=1.0):
        """
        Initialize the UncertaintySimilarity module.

        Args:
            hidden_dim (int): Hidden dimension for internal representations.
            epsilon (float): Small value to ensure numerical stability (default: 1e-6).
            temperature (float): Temperature parameter for confidence scaling (default: 1.0).
            use_sigmoid (bool): Whether to apply Sigmoid to the final similarity score (default: True).
        """
        super().__init__()
        
        # Shared feature transformation layer
        self.feature_transform = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU()
        )

        # Head for predicting mean matching probability
        self.mean_head = nn.Linear(hidden_dim, 1)
        nn.init.xavier_uniform_(self.mean_head.weight, gain=0.01)
        nn.init.zeros_(self.mean_head.bias)

        # Head for predicting bounded log variance.
        self.var_head = nn.Linear(hidden_dim, 1)
        nn.init.xavier_uniform_(self.var_head.weight, gain=0.01)
        nn.init.constant_(self.var_head.bias, -5.0)

        self.epsilon = epsilon
        self.temperature = temperature

    def forward(self, paired_features):
        """
        Forward pass for uncertainty-aware similarity computation.

        Args:
        paired_features (torch.Tensor): Input tensor of shape [..., hidden_dim * 2 + pair_extra_dim].

        Returns:
            dict: Dictionary containing:
                - 'similarity': Final similarity scores of shape [..., 1].
                - 'mean': Predicted mean similarity scores of shape [..., 1].
                - 'variance': Predicted variance of shape [..., 1].
                - 'uncertainty': Predicted uncertainty (standard deviation) of shape [..., 1].
                - 'confidence': Confidence scores based on uncertainty of shape [..., 1].
        """
        # Transform input features
        features = self.feature_transform(paired_features)

        # Paper UFM used by the delivered checkpoint.  This implementation is
        # intentionally kept here instead of being applied as a runtime monkey
        # patch, so importing the model cannot silently select another formula.
        mean_logits = self.mean_head(features)
        mean = torch.sigmoid(mean_logits)
        log_variance = torch.clamp(self.var_head(features), min=-10.0, max=10.0)
        variance = torch.exp(log_variance) + self.epsilon
        
        # Compute standard deviation (uncertainty)
        uncertainty = torch.sqrt(variance)
        
        confidence = torch.exp(-uncertainty * self.temperature) # Confidence factor

        # Uncertainty-aware similarity score S_ij = mu_ij * C_ij.
        similarity = mean * confidence

        return {
            'similarity': similarity,       # Final similarity score
            'logits': mean_logits,          # Raw logits for stable training
            'mean': mean,                   # Predicted mean
            'variance': variance,           # Predicted variance
            'uncertainty': uncertainty,     # Predicted uncertainty (std dev)
            'confidence': confidence        # Confidence based on uncertainty
        }
    

class TrajectoryMatchingNet(nn.Module):
    """Main network for matching trajectories between two datasets using temporal and spatial attention."""
    def __init__(
        self,
        input_dim,
        hidden_dim=128,
        gat_heads=4,
        uncertainty_temperature=1.25,
        pair_features=None,
        use_embedding_features=True,
        temporal_scales=None,
    ):
        """
        Initialize the TrajectoryMatchingNet.

        Args:
            input_dim (int): Dimension of input trajectory features (e.g., X, Y, speed, course).
            hidden_dim (int): Hidden dimension for internal representations (default: 128).
            gat_heads (int): Number of attention heads for GAT layers (default: 4).
            uncertainty_temperature (float): Temperature for uncertainty scaling (default: 1.0).
            路 When temperature = 1.0 (default): uncertainty affects confidence at its original scale
            路 When temperature > 1.0: the effect of uncertainty is amplified, making the model more sensitive to uncertainty
            路 When temperature < 1.0: the effect of uncertainty is reduced, making the model less sensitive to uncertainty
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self._edge_index_cache = {}
        self.pair_features = parse_pair_features(pair_features)
        self.pair_extra_dim = len(self.pair_features)
        self.use_embedding_features = use_embedding_features
        self.temporal_scales = parse_temporal_scales(temporal_scales)
        self.sinkhorn_geometry_logit = nn.Parameter(torch.tensor(-10.0))
        self.similarity_input_dim = (
            (hidden_dim * 2 if self.use_embedding_features else 0) + self.pair_extra_dim
        )
        if self.similarity_input_dim <= 0:
            raise ValueError("At least one of h_A/h_R or pair_features must be enabled.")

        # Feature extraction layers for datasets A and V
        self.feature_input_A = nn.Sequential(nn.Linear(input_dim, hidden_dim), nn.ReLU())
        self.feature_input_V = nn.Sequential(nn.Linear(input_dim, hidden_dim), nn.ReLU())

        # Graph Attention Network (GAT) layers for temporal graph processing
        self.gat_A = GATConv(hidden_dim, hidden_dim, heads=gat_heads, concat=False)
        self.gat_V = GATConv(hidden_dim, hidden_dim, heads=gat_heads, concat=False)
        
        # Temporal attention layers to aggregate features across time
        self.temporal_attention_A = TemporalAttention(hidden_dim)
        self.temporal_attention_V = TemporalAttention(hidden_dim)
        self.scale_attention_A = nn.Linear(hidden_dim, 1)
        self.scale_attention_V = nn.Linear(hidden_dim, 1)

        # Spatial attention layers to model interactions between trajectories
        self.spatial_attention_A = SpatialAttention(hidden_dim=hidden_dim)
        self.spatial_attention_V = SpatialAttention(hidden_dim=hidden_dim)
        
        # Feed-forward networks to fuse temporal and spatial features
        self.ffn_A = FeedForwardNetwork(hidden_dim * 2, hidden_dim)
        self.ffn_V = FeedForwardNetwork(hidden_dim * 2, hidden_dim)

        # Uncertainty-aware similarity computation
        self.similarity = UncertaintySimilarity(
            hidden_dim,
            input_dim=self.similarity_input_dim,
            temperature=uncertainty_temperature
        )

    def set_learned_sinkhorn_geometry_weight(self, initial_weight):
        initial_weight = max(float(initial_weight), 1e-6)
        initial = torch.tensor(
            initial_weight,
            dtype=self.sinkhorn_geometry_logit.dtype,
            device=self.sinkhorn_geometry_logit.device,
        )
        with torch.no_grad():
            self.sinkhorn_geometry_logit.copy_(torch.log(torch.expm1(initial)))

    def learned_sinkhorn_geometry_weight(self):
        return F.softplus(self.sinkhorn_geometry_logit)

    def _get_temporal_edge_index(self, num_nodes, scale, device):
        cache_key = (num_nodes, scale, str(device))
        edge_index = self._edge_index_cache.get(cache_key)
        if edge_index is None:
            edge_index = build_temporal_edge_index(num_nodes, scale).to(device)
            self._edge_index_cache[cache_key] = edge_index
        return edge_index

    def _encode_temporal(self, traj, feature_input, gat_layer, temporal_attention, scale_attention):
        num_nodes, seq_len, _ = traj.shape
        scales = self.temporal_scales or [seq_len]
        if max(scales) > seq_len:
            raise ValueError(f"temporal_scales {scales} cannot exceed input sequence length {seq_len}.")

        scale_features = []
        for scale in scales:
            traj_scale = traj[:, -scale:, :]
            feat = feature_input(traj_scale)
            edge_index = self._get_temporal_edge_index(num_nodes, scale, traj.device)
            feat_gnn = gat_layer(
                feat.reshape(num_nodes * scale, self.hidden_dim),
                edge_index,
            ).reshape(num_nodes, scale, self.hidden_dim)
            scale_features.append(temporal_attention(feat_gnn + feat))

        if len(scale_features) == 1:
            return scale_features[0]

        stacked_features = torch.stack(scale_features, dim=1)
        weights = torch.softmax(scale_attention(stacked_features), dim=1)
        return (stacked_features * weights).sum(dim=1)

    @staticmethod
    def _last_speed(traj):
        if traj.size(-1) >= 3:
            return traj[:, -1, 2:3]
        if traj.size(1) >= 2:
            return torch.norm(traj[:, -1, :2] - traj[:, -2, :2], dim=-1, keepdim=True)
        return torch.zeros(traj.size(0), 1, device=traj.device, dtype=traj.dtype)

    def _build_pair_extra_features(self, traj_A, traj_B):
        pos_A = traj_A[:, -1, :2]
        pos_B = traj_B[:, -1, :2]
        delta_xy = pos_A.unsqueeze(1) - pos_B.unsqueeze(0)
        pair_features = []

        if "dx" in self.pair_features:
            pair_features.append(delta_xy[..., 0:1])
        if "dy" in self.pair_features:
            pair_features.append(delta_xy[..., 1:2])

        if "distance" in self.pair_features or "distance2" in self.pair_features:
            distance = torch.norm(delta_xy, dim=-1, keepdim=True)
            distance = distance / distance.detach().mean().clamp_min(1e-6)
            if "distance" in self.pair_features:
                pair_features.append(distance)
            if "distance2" in self.pair_features:
                pair_features.append(distance.square())

        if "theta" in self.pair_features:
            theta = torch.atan2(delta_xy[..., 1:2], delta_xy[..., 0:1]) / torch.pi
            pair_features.append(theta)

        if "speed_diff" in self.pair_features:
            speed_A = self._last_speed(traj_A)
            speed_B = self._last_speed(traj_B)
            speed_diff = torch.abs(speed_A.unsqueeze(1) - speed_B.unsqueeze(0))
            speed_diff = speed_diff / speed_diff.detach().mean().clamp_min(1e-6)
            pair_features.append(speed_diff)

        if not pair_features:
            return delta_xy.new_zeros(delta_xy.size(0), delta_xy.size(1), 0)
        return torch.cat(pair_features, dim=-1)

    def _spatial_attention_batch(self, spatial_layer, query_features, context_features):
        batch_size, num_query, hidden_dim = query_features.shape
        num_context = context_features.size(1)
        query = query_features.reshape(batch_size * num_query, 1, hidden_dim)
        key_value = (
            context_features.unsqueeze(1)
            .expand(batch_size, num_query, num_context, hidden_dim)
            .reshape(batch_size * num_query, num_context, hidden_dim)
        )
        attn_out, _ = spatial_layer.spatial_attn(query, key_value, key_value)
        return attn_out.squeeze(1).reshape(batch_size, num_query, hidden_dim) + query_features

    def _build_pair_extra_features_batch(self, traj_A, traj_B):
        pos_A = traj_A[:, :, -1, :2]
        pos_B = traj_B[:, :, -1, :2]
        delta_xy = pos_A.unsqueeze(2) - pos_B.unsqueeze(1)
        pair_features = []

        if "dx" in self.pair_features:
            pair_features.append(delta_xy[..., 0:1])
        if "dy" in self.pair_features:
            pair_features.append(delta_xy[..., 1:2])

        if "distance" in self.pair_features or "distance2" in self.pair_features:
            distance = torch.norm(delta_xy, dim=-1, keepdim=True)
            distance_mean = distance.detach().mean(dim=(1, 2), keepdim=True).clamp_min(1e-6)
            distance = distance / distance_mean
            if "distance" in self.pair_features:
                pair_features.append(distance)
            if "distance2" in self.pair_features:
                pair_features.append(distance.square())

        if "theta" in self.pair_features:
            theta = torch.atan2(delta_xy[..., 1:2], delta_xy[..., 0:1]) / torch.pi
            pair_features.append(theta)

        if "speed_diff" in self.pair_features:
            speed_A = self._last_speed(traj_A.reshape(-1, traj_A.size(2), traj_A.size(3))).reshape(
                traj_A.size(0), traj_A.size(1), 1
            )
            speed_B = self._last_speed(traj_B.reshape(-1, traj_B.size(2), traj_B.size(3))).reshape(
                traj_B.size(0), traj_B.size(1), 1
            )
            speed_diff = torch.abs(speed_A.unsqueeze(2) - speed_B.unsqueeze(1))
            speed_mean = speed_diff.detach().mean(dim=(1, 2), keepdim=True).clamp_min(1e-6)
            pair_features.append(speed_diff / speed_mean)

        if not pair_features:
            return delta_xy.new_zeros(delta_xy.size(0), delta_xy.size(1), delta_xy.size(2), 0)
        return torch.cat(pair_features, dim=-1)

    def forward_batch_same_shape(self, traj_A, traj_B, return_uncertainty=False):
        batch_size, num_A, seq_len, input_dim = traj_A.shape
        _, num_B, _, _ = traj_B.shape
        flat_A = traj_A.reshape(batch_size * num_A, seq_len, input_dim)
        flat_B = traj_B.reshape(batch_size * num_B, seq_len, input_dim)

        feat_A_temporal = self._encode_temporal(
            flat_A,
            self.feature_input_A,
            self.gat_A,
            self.temporal_attention_A,
            self.scale_attention_A,
        ).reshape(batch_size, num_A, self.hidden_dim)
        feat_B_temporal = self._encode_temporal(
            flat_B,
            self.feature_input_V,
            self.gat_V,
            self.temporal_attention_V,
            self.scale_attention_V,
        ).reshape(batch_size, num_B, self.hidden_dim)

        feat_A_spatial = self._spatial_attention_batch(
            self.spatial_attention_A,
            feat_A_temporal,
            feat_A_temporal,
        )
        feat_B_spatial = self._spatial_attention_batch(
            self.spatial_attention_V,
            feat_B_temporal,
            feat_B_temporal,
        )

        feat_A_concat = torch.cat([feat_A_temporal, feat_A_spatial], dim=-1)
        feat_B_concat = torch.cat([feat_B_temporal, feat_B_spatial], dim=-1)
        feat_A_final = (
            self.ffn_A(feat_A_concat.reshape(batch_size * num_A, -1)).reshape(batch_size, num_A, self.hidden_dim)
            + feat_A_temporal
            + feat_A_spatial
        )
        feat_B_final = (
            self.ffn_V(feat_B_concat.reshape(batch_size * num_B, -1)).reshape(batch_size, num_B, self.hidden_dim)
            + feat_B_temporal
            + feat_B_spatial
        )

        paired_parts = []
        if self.use_embedding_features:
            feat_A_expanded = feat_A_final.unsqueeze(2).expand(batch_size, num_A, num_B, -1)
            feat_B_expanded = feat_B_final.unsqueeze(1).expand(batch_size, num_A, num_B, -1)
            paired_parts.extend([feat_A_expanded, feat_B_expanded])
        pair_extra = self._build_pair_extra_features_batch(traj_A, traj_B)
        if self.pair_extra_dim > 0:
            paired_parts.append(pair_extra)
        paired_features = torch.cat(paired_parts, dim=-1)

        similarity_output = self.similarity(paired_features.reshape(-1, self.similarity_input_dim))
        similarity_matrix = similarity_output["similarity"].reshape(batch_size, num_A, num_B)

        if return_uncertainty:
            return {
                "similarity": similarity_matrix,
                "logits": similarity_output["logits"].reshape(batch_size, num_A, num_B),
                "mean": similarity_output["mean"].reshape(batch_size, num_A, num_B),
                "uncertainty": similarity_output["uncertainty"].reshape(batch_size, num_A, num_B),
                "confidence": similarity_output["confidence"].reshape(batch_size, num_A, num_B),
                "embedding_A": feat_A_final,
                "embedding_B": feat_B_final,
            }
        return similarity_matrix

    def forward(self, traj_A, traj_B, return_uncertainty=False):
        """
        Forward pass for trajectory matching.

        Args:
            traj_A (torch.Tensor): Trajectories from dataset A, shape [N_A, seq_len, input_dim].
            traj_B (torch.Tensor): Trajectories from dataset B, shape [N_B, seq_len, input_dim].
            return_uncertainty (bool): Whether to return uncertainty information (default: True).

        Returns:
            dict or torch.Tensor: 
                If return_uncertainty is True, returns a dictionary containing:
                    - 'similarity': Similarity matrix of shape [N_A, N_B]
                    - 'uncertainty': Uncertainty matrix of shape [N_A, N_B]
                    - 'confidence': Confidence matrix of shape [N_A, N_B]
                If return_uncertainty is False, returns only the similarity matrix.
        """

        N_A, seq_len, _ = traj_A.shape
        N_B, _, _ = traj_B.shape

        # Apply temporal graph attention at one or more scales.
        feat_A_temporal = self._encode_temporal(
            traj_A,
            self.feature_input_A,
            self.gat_A,
            self.temporal_attention_A,
            self.scale_attention_A,
        )
        feat_B_temporal = self._encode_temporal(
            traj_B,
            self.feature_input_V,
            self.gat_V,
            self.temporal_attention_V,
            self.scale_attention_V,
        )
    
        # Apply spatial attention to model interactions between trajectories
        feat_A_spatial = self.spatial_attention_A(feat_A_temporal, feat_A_temporal)  # Shape: [N_A, hidden_dim]
        feat_B_spatial = self.spatial_attention_V(feat_B_temporal, feat_B_temporal)  # Shape: [N_B, hidden_dim]
 
        # Concatenate temporal and spatial features
        feat_A_concat = torch.cat([feat_A_temporal, feat_A_spatial], dim=-1)  # Shape: [N_A, hidden_dim*2]
        feat_B_concat = torch.cat([feat_B_temporal, feat_B_spatial], dim=-1)  # Shape: [N_B, hidden_dim*2]
    
        # Fuse features using feed-forward networks with residual connections
        feat_A_final = self.ffn_A(feat_A_concat) + feat_A_temporal + feat_A_spatial  # Shape: [N_A, hidden_dim]
        feat_B_final = self.ffn_V(feat_B_concat) + feat_B_temporal + feat_B_spatial  # Shape: [N_B, hidden_dim]

        # Compute similarity scores with uncertainty for all pairs of trajectories
        paired_parts = []
        if self.use_embedding_features:
            feat_A_expanded = feat_A_final.unsqueeze(1).expand(N_A, N_B, -1)  # Shape: [N_A, N_B, hidden_dim]
            feat_B_expanded = feat_B_final.unsqueeze(0).expand(N_A, N_B, -1)  # Shape: [N_A, N_B, hidden_dim]
            paired_parts.extend([feat_A_expanded, feat_B_expanded])
        pair_extra = self._build_pair_extra_features(traj_A, traj_B)
        if self.pair_extra_dim > 0:
            paired_parts.append(pair_extra)
        paired_features = torch.cat(paired_parts, dim=2)
        
        # Get uncertainty-aware similarity scores
        similarity_output = self.similarity(
            paired_features.view(-1, self.similarity_input_dim)
        )
        
        # Reshape outputs to matrix form
        similarity_matrix = similarity_output['similarity'].view(N_A, N_B)
        
        if return_uncertainty:
            uncertainty_matrix = similarity_output['uncertainty'].view(N_A, N_B)
            confidence_matrix = similarity_output['confidence'].view(N_A, N_B)
            mean_matrix = similarity_output['mean'].view(N_A, N_B)
            logits_matrix = similarity_output['logits'].view(N_A, N_B)
            
            return {
                'similarity': similarity_matrix,
                'logits': logits_matrix,
                'mean': mean_matrix,
                'uncertainty': uncertainty_matrix,
                'confidence': confidence_matrix,
                'embedding_A': feat_A_final,
                'embedding_B': feat_B_final
            }
        else:
            return similarity_matrix


class AsyncSegmentEncoder(nn.Module):
    """Encode one variable-length trajectory segment with temporal graph edges."""

    def __init__(self, input_dim, hidden_dim=128, gat_heads=4):
        super().__init__()
        if input_dim < 2:
            raise ValueError("AsyncSegmentEncoder expects spatial features plus a relative-time feature.")
        if hidden_dim % gat_heads != 0:
            raise ValueError("hidden_dim must be divisible by gat_heads.")
        self.hidden_dim = hidden_dim
        self.spatial_input = nn.Sequential(nn.Linear(input_dim - 1, hidden_dim), nn.ReLU())
        self.time_input = nn.Sequential(nn.Linear(1, hidden_dim), nn.ReLU())
        self.gat = GATConv(hidden_dim, hidden_dim // gat_heads, heads=gat_heads)
        self.spatial_pool_gate = nn.Linear(hidden_dim, 1)
        self.time_pool_gate = nn.Linear(hidden_dim, 1)
        self.spatial_fusion = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.time_fusion = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )

    @staticmethod
    def _temporal_edges(num_nodes, device):
        if num_nodes <= 1:
            return torch.empty((2, 0), dtype=torch.long, device=device)
        src = torch.arange(num_nodes - 1, device=device, dtype=torch.long)
        dst = src + 1
        return torch.cat(
            [
                torch.stack([src, dst], dim=0),
                torch.stack([dst, src], dim=0),
            ],
            dim=1,
        )

    def _pack_segments(self, segments):
        device = segments[0].device
        lengths = []
        edge_parts = []
        last_indices = []
        offset = 0
        for segment in segments:
            if segment.ndim != 2 or segment.size(0) == 0:
                raise ValueError("Each async segment must have shape [num_points, input_dim].")
            length = int(segment.size(0))
            lengths.append(length)
            last_indices.append(offset + length - 1)
            edges = self._temporal_edges(length, device)
            if edges.numel() > 0:
                edge_parts.append(edges + offset)
            offset += length

        node_features = torch.cat(segments, dim=0)
        segment_ids = torch.repeat_interleave(
            torch.arange(len(segments), device=device, dtype=torch.long),
            torch.tensor(lengths, device=device, dtype=torch.long),
        )
        edge_index = (
            torch.cat(edge_parts, dim=1)
            if edge_parts
            else torch.empty((2, 0), dtype=torch.long, device=device)
        )
        last_indices = torch.tensor(last_indices, device=device, dtype=torch.long)
        return node_features, segment_ids, edge_index, last_indices

    @staticmethod
    def _segment_pool(features, scores, segment_ids, num_segments):
        weights = softmax(scores, segment_ids, num_nodes=num_segments)
        pooled = features.new_zeros(num_segments, features.size(-1))
        pooled.index_add_(0, segment_ids, features * weights.unsqueeze(-1))
        return pooled

    def forward(self, segments):
        if not segments:
            raise ValueError("AsyncSegmentEncoder received an empty segment list.")
        nodes, segment_ids, edge_index, last_indices = self._pack_segments(segments)
        num_segments = len(segments)

        spatial_features = self.spatial_input(nodes[:, :-1])
        time_features = self.time_input(nodes[:, -1:])
        graph_features = self.gat(spatial_features, edge_index)

        spatial_scores = self.spatial_pool_gate(graph_features).squeeze(-1)
        spatial_pooled = self._segment_pool(
            graph_features,
            spatial_scores,
            segment_ids,
            num_segments,
        )
        spatial_last = graph_features[last_indices]
        spatial_embedding = (
            self.spatial_fusion(torch.cat([spatial_pooled, spatial_last], dim=-1))
            + spatial_pooled
        )

        time_scores = self.time_pool_gate(time_features).squeeze(-1)
        time_pooled = self._segment_pool(
            time_features,
            time_scores,
            segment_ids,
            num_segments,
        )
        time_last = time_features[last_indices]
        time_embedding = self.time_fusion(torch.cat([time_pooled, time_last], dim=-1)) + time_pooled
        return {
            "space": spatial_embedding,
            "time": time_embedding,
            "combined": spatial_embedding + time_embedding,
        }


class AsyncMultiRateMatchingNet(nn.Module):
    """Match AIS intervals to asynchronous Radar segments without forcing equal lengths."""

    def __init__(
        self,
        input_dim,
        hidden_dim=128,
        gat_heads=4,
        pair_features=None,
        use_embedding_features=True,
    ):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.pair_features = parse_pair_features(pair_features)
        self.pair_extra_dim = len(self.pair_features)
        self.use_embedding_features = use_embedding_features

        self.encoder_A = AsyncSegmentEncoder(input_dim, hidden_dim, gat_heads)
        self.encoder_B = AsyncSegmentEncoder(input_dim, hidden_dim, gat_heads)
        self.context_A = SpatialAttention(hidden_dim, num_heads=gat_heads)
        self.context_B = SpatialAttention(hidden_dim, num_heads=gat_heads)
        self.ffn_A = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.ffn_B = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )

        embedding_dim = hidden_dim * 4 if use_embedding_features else 0
        self.similarity_input_dim = embedding_dim + self.pair_extra_dim
        if self.similarity_input_dim <= 0:
            raise ValueError("At least one embedding or pair feature input is required.")
        self.similarity = UncertaintySimilarity(hidden_dim, self.similarity_input_dim)

    @staticmethod
    def _state_speed(states):
        if states.size(-1) >= 3:
            return states[:, 2:3]
        return states.new_zeros(states.size(0), 1)

    def _build_pair_extra_features(self, states_A, states_B):
        pos_A = states_A[:, :2]
        pos_B = states_B[:, :2]
        delta_xy = pos_A.unsqueeze(1) - pos_B.unsqueeze(0)
        pair_features = []

        if "dx" in self.pair_features:
            pair_features.append(delta_xy[..., 0:1])
        if "dy" in self.pair_features:
            pair_features.append(delta_xy[..., 1:2])

        if "distance" in self.pair_features or "distance2" in self.pair_features:
            distance = torch.norm(delta_xy, dim=-1, keepdim=True)
            distance = distance / distance.detach().mean().clamp_min(1e-6)
            if "distance" in self.pair_features:
                pair_features.append(distance)
            if "distance2" in self.pair_features:
                pair_features.append(distance.square())

        if "theta" in self.pair_features:
            theta = torch.atan2(delta_xy[..., 1:2], delta_xy[..., 0:1]) / torch.pi
            pair_features.append(theta)

        if "speed_diff" in self.pair_features:
            speed_A = self._state_speed(states_A)
            speed_B = self._state_speed(states_B)
            speed_diff = torch.abs(speed_A.unsqueeze(1) - speed_B.unsqueeze(0))
            speed_diff = speed_diff / speed_diff.detach().mean().clamp_min(1e-6)
            pair_features.append(speed_diff)

        if not pair_features:
            return delta_xy.new_zeros(delta_xy.size(0), delta_xy.size(1), 0)
        return torch.cat(pair_features, dim=-1)

    def _forward_encoded(self, encoded_A, encoded_B, states_A, states_B, return_uncertainty=False):
        context_A = self.context_A(encoded_A["combined"], encoded_A["combined"])
        context_B = self.context_B(encoded_B["combined"], encoded_B["combined"])
        space_A = self.ffn_A(torch.cat([encoded_A["space"], context_A], dim=-1)) + encoded_A["space"] + context_A
        space_B = self.ffn_B(torch.cat([encoded_B["space"], context_B], dim=-1)) + encoded_B["space"] + context_B
        time_A = encoded_A["time"]
        time_B = encoded_B["time"]
        feature_A = torch.cat([time_A, space_A], dim=-1)
        feature_B = torch.cat([time_B, space_B], dim=-1)

        num_A, num_B = feature_A.size(0), feature_B.size(0)
        paired_parts = []
        if self.use_embedding_features:
            paired_parts.append(feature_A.unsqueeze(1).expand(num_A, num_B, -1))
            paired_parts.append(feature_B.unsqueeze(0).expand(num_A, num_B, -1))
        if self.pair_extra_dim > 0:
            paired_parts.append(self._build_pair_extra_features(states_A, states_B))
        paired_features = torch.cat(paired_parts, dim=-1)

        similarity_output = self.similarity(
            paired_features.reshape(-1, self.similarity_input_dim)
        )
        similarity_matrix = similarity_output["similarity"].reshape(num_A, num_B)
        if not return_uncertainty:
            return similarity_matrix
        return {
            "similarity": similarity_matrix,
            "logits": similarity_output["logits"].reshape(num_A, num_B),
            "mean": similarity_output["mean"].reshape(num_A, num_B),
            "uncertainty": similarity_output["uncertainty"].reshape(num_A, num_B),
            "confidence": similarity_output["confidence"].reshape(num_A, num_B),
            "embedding_A": feature_A,
            "embedding_B": feature_B,
            "time_A": time_A,
            "time_B": time_B,
            "space_A": space_A,
            "space_B": space_B,
        }

    def forward(self, segments_A, segments_B, states_A, states_B, return_uncertainty=False):
        encoded_A = self.encoder_A(segments_A)
        encoded_B = self.encoder_B(segments_B)
        return self._forward_encoded(
            encoded_A,
            encoded_B,
            states_A,
            states_B,
            return_uncertainty=return_uncertainty,
        )

    @staticmethod
    def _slice_encoded(encoded, start, end):
        return {
            key: value[start:end]
            for key, value in encoded.items()
        }

    def forward_windows(self, windows, return_uncertainty=False):
        all_segments_A = []
        all_segments_B = []
        counts_A = []
        counts_B = []
        for window in windows:
            counts_A.append(len(window.segments_A))
            counts_B.append(len(window.segments_B))
            all_segments_A.extend(window.segments_A)
            all_segments_B.extend(window.segments_B)

        encoded_A = self.encoder_A(all_segments_A)
        encoded_B = self.encoder_B(all_segments_B)
        outputs = []
        offset_A = 0
        offset_B = 0
        for window, count_A, count_B in zip(windows, counts_A, counts_B):
            next_A = offset_A + count_A
            next_B = offset_B + count_B
            outputs.append(
                self._forward_encoded(
                    self._slice_encoded(encoded_A, offset_A, next_A),
                    self._slice_encoded(encoded_B, offset_B, next_B),
                    window.states_A,
                    window.states_B,
                    return_uncertainty=return_uncertainty,
                )
            )
            offset_A = next_A
            offset_B = next_B
        return outputs

