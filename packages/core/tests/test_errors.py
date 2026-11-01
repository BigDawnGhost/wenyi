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


def test_agent_errors_join_the_shared_base_but_keep_their_parents():
    """Agents raise these into CLI guards; both parents must keep matching."""
    from wenyi_core.agents.annotation_aligner import AnnotationAlignmentError
    from wenyi_core.agents.precision import PrecisionError
    from wenyi_core.agents.review_actions import ReviewLoopProtocolError
    from wenyi_core.agents.review_fixer import ReviewFixerProtocolError
    from wenyi_core.agents.reviewer import ReviewOutputError
    from wenyi_core.agents.terminology import (
        TerminologyKnowledgeError,
        TerminologyReferenceError,
    )
    from wenyi_core.agents.title_translator import TitleOutputError

    value_errors = [
        AnnotationAlignmentError,
        PrecisionError,
        ReviewLoopProtocolError,
        ReviewFixerProtocolError,
        ReviewOutputError,
        TerminologyKnowledgeError,
        TerminologyReferenceError,
    ]
    for error_type in value_errors:
        assert issubclass(error_type, WenyiError), error_type
        assert issubclass(error_type, ValueError), error_type
    assert issubclass(TitleOutputError, WenyiError)
    assert issubclass(TitleOutputError, RuntimeError)


def test_agents_exceptions_are_catchable_by_the_cli_guard():
    """The CLI catches WenyiError so it can print one line instead of a traceback."""
    from wenyi_core.agents.precision import PrecisionError

    try:
        raise PrecisionError("bad precision output")
    except WenyiError as error:
        assert str(error) == "bad precision output"
    else:
        pytest.fail("agent failures must be catchable as WenyiError")
