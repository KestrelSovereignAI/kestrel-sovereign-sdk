"""Unknown delivery facts survive explicit nesting without inventing authority."""

from kestrel_sdk.resources import ResourceConflict, ResourceOutcomeUnknown, observed_outcome_unknown


def test_explicit_cause_and_group_keep_each_original_once_even_with_cycles():
    original = ResourceOutcomeUnknown('Original delivery uncertain')
    wrapped = ResourceConflict('Original precondition result')
    wrapped.__cause__ = original
    grouped = ExceptionGroup('Actual siblings', [wrapped, original])
    original.__cause__ = grouped
    assert observed_outcome_unknown(grouped) == (original,)
    assert original.__cause__ is grouped and wrapped.__cause__ is original


def test_incidental_handling_context_does_not_claim_an_unknown_outcome():
    original = ResourceOutcomeUnknown('An unrelated earlier operation')
    ordinary = ValueError('Current known failure')
    ordinary.__context__ = original
    assert observed_outcome_unknown(ordinary) == ()
    assert observed_outcome_unknown(None) == ()


def test_application_attribute_hooks_cannot_hide_original_group_evidence():
    original = ResourceOutcomeUnknown('Native group member')

    class HiddenGroup(ExceptionGroup):
        @property
        def exceptions(self):
            raise AssertionError('Application exception hook ran')

        @property
        def __cause__(self):
            raise AssertionError('Application cause hook ran')

    grouped = HiddenGroup('Opaque application carrier', [original])
    assert observed_outcome_unknown(grouped) == (original,)


def test_application_class_claim_cannot_invent_an_unknown_resource_error():
    class SpoofedConflict(ResourceConflict):
        @property
        def __class__(self):
            return ResourceOutcomeUnknown

    ordinary = SpoofedConflict('Known precondition refusal')
    assert observed_outcome_unknown(ordinary) == ()
