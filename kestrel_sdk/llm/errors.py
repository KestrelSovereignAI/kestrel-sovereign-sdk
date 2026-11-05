"""Provider protocol failures shared by SDK-only adapters and their hosts."""


class LLMProtocolError(RuntimeError):
    """A provider response violates the adapter protocol.

    Use a content-free description of the violated rule. Hosts must treat
    this as a failed generation, never as completion evidence or authority
    to execute tools. Transport failures and cancellation retain their own
    exception types.
    """
