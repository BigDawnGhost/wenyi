"""The shared backend export is the single credential-redaction exit."""

import pytest
from wenyi_backend.security import CredentialRedactor, validate_credential
from wenyi_core.llm.credentials import CredentialRedactor as CoreCredentialRedactor


def test_shared_export_is_the_core_implementation():
    assert CredentialRedactor is CoreCredentialRedactor


def test_redactor_masks_every_snapshot_secret_longest_first():
    redactor = CredentialRedactor(["sk-short", "sk-much-longer-secret", None, ""])

    assert redactor("head sk-much-longer-secret middle sk-short tail") == (
        "head [redacted] middle [redacted] tail"
    )


def test_redactor_replaces_repeated_occurrences_and_keeps_other_text():
    redactor = CredentialRedactor(["sk-secret"])

    assert redactor("a sk-secret b sk-secret c") == "a [redacted] b [redacted] c"
    assert redactor("plain diagnostic text") == "plain diagnostic text"


def test_validate_credential_accepts_normal_keys_and_rejects_control_characters():
    validate_credential("sk-normal-key")
    validate_credential(None)

    with pytest.raises(ValueError):
        validate_credential("sk-invalid\nkey")
