"""Pure operational logic.

Everything in this package is deterministic and free of LLM calls: forecasting,
reorder points, BOM costing, margin analysis, roster fitting and reconciliation.
Agents reach this code through tools, so the arithmetic that drives money
decisions is unit-testable and reproducible on its own.
"""
