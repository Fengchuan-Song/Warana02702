"""Partial Sinkhorn scoring and Hungarian decoding used by the mainline."""

from typing import List, Tuple

import torch
from scipy.optimize import linear_sum_assignment


def geometry_guidance(ais_features: torch.Tensor, radar_features: torch.Tensor) -> torch.Tensor:
    ais_positions = ais_features[:, -1, :2]
    radar_positions = radar_features[:, -1, :2]
    distances = torch.norm(ais_positions.unsqueeze(1) - radar_positions.unsqueeze(0), dim=-1)
    return distances / distances.detach().mean().clamp_min(1e-6)


def log_sinkhorn(
    log_scores: torch.Tensor,
    log_row_marginals: torch.Tensor,
    log_col_marginals: torch.Tensor,
    iterations: int,
) -> torch.Tensor:
    row_dual = torch.zeros_like(log_row_marginals)
    col_dual = torch.zeros_like(log_col_marginals)
    for _ in range(iterations):
        row_dual = log_row_marginals - torch.logsumexp(
            log_scores + col_dual.unsqueeze(0), dim=1
        )
        col_dual = log_col_marginals - torch.logsumexp(
            log_scores + row_dual.unsqueeze(1), dim=0
        )
    return log_scores + row_dual.unsqueeze(1) + col_dual.unsqueeze(0)


def partial_sinkhorn_scores(
    logits: torch.Tensor,
    geometry: torch.Tensor,
    geometry_weight: float = 0.01,
    iterations: int = 20,
    dustbin_score: float = 0.0,
) -> torch.Tensor:
    """Return the real-pair block of the dustbin-augmented log assignment."""
    scores = logits - float(geometry_weight) * geometry.detach()
    num_ais, num_radar = scores.shape
    dustbin_row = scores.new_full((1, num_radar), dustbin_score)
    dustbin_col = scores.new_full((num_ais + 1, 1), dustbin_score)
    augmented = torch.cat([torch.cat([scores, dustbin_row], dim=0), dustbin_col], dim=1)

    row_marginals = scores.new_ones(num_ais + 1)
    row_marginals[-1] = max(num_radar, 1)
    col_marginals = scores.new_ones(num_radar + 1)
    col_marginals[-1] = max(num_ais, 1)
    assignment = log_sinkhorn(
        augmented,
        torch.log(row_marginals),
        torch.log(col_marginals),
        iterations,
    )
    return assignment[:num_ais, :num_radar]


def decode_hungarian(log_assignment: torch.Tensor, threshold: float) -> List[Tuple[int, int, float]]:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("match threshold must be between 0 and 1")
    if log_assignment.numel() == 0:
        return []
    probabilities = log_assignment.exp()
    row_indices, col_indices = linear_sum_assignment(-log_assignment.detach().cpu().numpy())
    matches = []
    for row_index, col_index in zip(row_indices, col_indices):
        confidence = float(probabilities[row_index, col_index].detach().cpu())
        if confidence > threshold:
            matches.append((int(row_index), int(col_index), confidence))
    return matches
