"""Custom tagging: natural-language requests -> validated, read-only tagging proposals.

Separate from policy-based compliance: a custom tag never becomes a mandatory policy tag.
"""

from cloudguard.governance.custom_tagging.engine import CustomTaggingEngine
from cloudguard.governance.custom_tagging.parser import CompositeParser, RequestParser, RuleBasedParser

__all__ = ["CompositeParser", "CustomTaggingEngine", "RequestParser", "RuleBasedParser"]
