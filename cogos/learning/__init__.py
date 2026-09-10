"""Experience learning kernel (L1-L3).

An external trainable decision layer around a frozen model. Nothing in here updates the hosted
model's weights — the three outcomes it distinguishes are *storing experience*, *changing the
external agent's policy*, and *training model parameters*, and only the first two happen here.
"""

from cogos.learning.experience import ExperienceBuilder, accept_for_learning, extract_features
from cogos.learning.policy import ContextualBandit, LinearSarsaQ, PolicyStore, constrained_actions
from cogos.learning.replay import ReplayStore, Split, retention_report

__all__ = [
    "ContextualBandit",
    "ExperienceBuilder",
    "LinearSarsaQ",
    "PolicyStore",
    "ReplayStore",
    "Split",
    "accept_for_learning",
    "constrained_actions",
    "extract_features",
    "retention_report",
]
