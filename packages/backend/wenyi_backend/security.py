"""Single shared credential-redaction export for Web and Desktop hosts.

`wenyi_core.llm.credentials` owns the implementation; this module is the one
import point platform code should use, so redaction behaviour cannot drift
between hosts.
"""

from wenyi_core.llm.credentials import CredentialRedactor, validate_credential

__all__ = ["CredentialRedactor", "validate_credential"]
