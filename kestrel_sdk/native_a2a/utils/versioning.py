# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Protocol compatibility is determined by the Major.Minor release."""

from packaging.version import Version


def major_minor(version: str) -> tuple[int, int]:
    """Return the protocol release, ignoring the specification patch number."""
    parsed = Version(version)
    return parsed.major, parsed.minor
