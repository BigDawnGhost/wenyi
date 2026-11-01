"""The shared error base must stay additive while the migration is in flight."""

import pytest
from wenyi_core.errors import WenyiError
from wenyi_core.ingest.errors import IngestError, MinerUError, MinerUTimeoutError
from wenyi_core.storage.sqlite import StorageBusyError


def test_ingest_errors_are_still_runtime_errors():
    """Existing except RuntimeError guards must keep matching."""
    assert issubclass(IngestError, RuntimeError)
    assert issubclass(IngestError, WenyiError)
    assert issubclass(MinerUError, WenyiError)
    assert issubclass(MinerUError, RuntimeError)
    assert issubclass(MinerUTimeoutError, WenyiError)
    assert issubclass(MinerUTimeoutError, IngestError)


def test_storage_busy_error_is_still_a_blocking_error():
    """Existing except BlockingIOError guards depend on this."""
    assert issubclass(StorageBusyError, BlockingIOError)
    assert issubclass(StorageBusyError, WenyiError)
    error = StorageBusyError("locked")
    assert isinstance(error, BlockingIOError)
    assert isinstance(error, WenyiError)


def test_instances_are_catchable_as_both_parents():
    try:
        raise MinerUTimeoutError("timed out")
    except WenyiError as error:
        assert error.message == "timed out"
    else:
        pytest.fail("a MinerU timeout must be catchable as WenyiError")

    try:
        raise StorageBusyError("busy")
    except BlockingIOError as error:
        assert error.message == "busy"
    else:
        pytest.fail("storage contention must stay catchable as BlockingIOError")


def test_str_returns_the_bare_message():
    """Callers and tests match on str(exc); it must not gain a code prefix."""
    assert str(WenyiError("plain")) == "plain"
    assert str(WenyiError()) == ""
    assert str(IngestError("bad input")) == "bad input"
    assert str(MinerUError("")) == ""


def test_default_code_and_retryable_are_declared_on_the_base():
    assert WenyiError.code == "wenyi_error"
    assert WenyiError.retryable is False
    assert IngestError.retryable is False


def test_per_instance_code_does_not_pollute_the_class():
    """An override belongs to the instance; the class attribute must survive."""
    error = WenyiError("boom", code="custom_code")
    assert error.code == "custom_code"
    assert WenyiError.code == "wenyi_error"
    # A later instance without an override still sees the class default.
    assert WenyiError("again").code == "wenyi_error"
    # Subclasses are unaffected too.
    assert IngestError("x", code="ingest_override").code == "ingest_override"
    assert IngestError.code == "wenyi_error"


def test_message_is_available_without_positional_arguments():
    """Existing code raises some of these with no message at all."""
    assert WenyiError().message == ""
    assert str(IngestError()) == ""


def test_retryable_can_be_declared_per_subclass():
    class Transient(WenyiError):
        code = "transient"
        retryable = True

    assert Transient.retryable is True
    assert WenyiError.retryable is False
