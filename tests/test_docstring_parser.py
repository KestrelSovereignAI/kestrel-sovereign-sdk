"""Regression tests for parse_docstring_params wrapped-description truncation.

Mirrors kestrel-sovereign #1925: the param terminator must not truncate a
multi-line Args description at the first continuation line beginning with a
word, or documented constraints never reach the agent-facing tool schema.
"""

from kestrel_sdk.features.base import parse_docstring_params


def test_wrapped_description_not_truncated_at_word_continuation():
    docstring = '''
    Configure the backend.

    Args:
        model: When backend is claude, one of opus, sonnet, or
            haiku. When backend is codex or opencode, a non-blank
            provider model id (blank is rejected).
        auth_config: Required keys depend on auth_type. For
            ip_allowlist pass allowed_ips as a list of CIDRs.
        flag: A trailing one-liner.

    Returns:
        The result.
    '''
    result = parse_docstring_params(docstring)
    # Tail content past the first word-starting continuation must survive.
    assert "opencode" in result["model"]
    assert "blank is rejected" in result["model"]
    assert "allowed_ips" in result["auth_config"]
    # Param boundaries stay correct.
    assert result["flag"] == "A trailing one-liner."
    assert "allowed_ips" not in result["flag"]
    assert "haiku" not in result["auth_config"]


def test_single_line_params_unchanged():
    docstring = '''
    Do something.

    Args:
        a: First param.
        b: Second param.
    '''
    result = parse_docstring_params(docstring)
    assert result["a"] == "First param."
    assert result["b"] == "Second param."


def test_param_with_type_annotation():
    docstring = '''
    Args:
        count (int): How many to process, wrapped onto
            a second line for good measure.
    '''
    result = parse_docstring_params(docstring)
    assert "second line" in result["count"]
