"""Internal experimental workflow kernel for the product orchestrator.

This package is not a second product entry point and is not the user-facing
"V2 executable".  The only product entry remains
``unified_paper_orchestrator`` / ``UnifiedPaperOrchestrator.exe``.  The package
name is retained internally to avoid a risky import migration while the
kernel, gateway, and SQLite experiments are still referenced by that product.
"""

__version__ = "0.1.0"
