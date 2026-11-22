"""Desktop credential handling must reuse the shared backend redaction export."""

import wenyi_desktop.local_credentials as local_credentials
from wenyi_backend.security import validate_credential as shared_validate_credential


def test_desktop_binds_the_shared_credential_export():
    assert local_credentials.validate_credential is shared_validate_credential
