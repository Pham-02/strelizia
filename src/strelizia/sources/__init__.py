"""Log sources. Each source yields `RawLine`s for the assembler."""


class SourceError(Exception):
    """A log source could not be read. The message is safe to show to the user."""
