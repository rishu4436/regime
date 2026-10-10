"""Bot 3 - Risk & Control Layer. Implements backend.contracts.RiskManager."""
from .config import RiskConfig, SymbolRiskRules
from .exits import Bar, ExitDecision, ExitLevels, compute_levels, evaluate_bar, max_loss_level
from .manager import RISK_EXIT_REASONS, DefaultRiskManager, is_risk_exit

__all__ = [
    "DefaultRiskManager", "RiskConfig", "SymbolRiskRules", "RISK_EXIT_REASONS", "is_risk_exit",
    "Bar", "ExitDecision", "ExitLevels", "compute_levels", "evaluate_bar", "max_loss_level",
]
