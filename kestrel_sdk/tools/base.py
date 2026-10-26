"""
Base classes and interfaces for Kestrel agent tools.

This module defines the core abstractions for the tool system, enabling agents
to autonomously select and execute capabilities based on context and user needs.
"""

import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class _CoercionFailed:
    """Singleton sentinel: a value is not representable as the declared type."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<coercion-failed>"


COERCION_FAILED = _CoercionFailed()

# Tokens accepted for a "boolean" parameter, shared by both argument paths.
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})

# The types this function actually converts, and so the only ones for which a
# JSON ``null`` is a type error rather than a pass-through value. "string",
# "object" and "array" values are handed over untouched, ``None`` included.
_COERCED_TYPES = frozenset({"integer", "number", "boolean"})


def coerce_json_value(value: Any, param_type: str, nullable: bool = False) -> Any:
    """
    Coerce a value to the JSON schema type its parameter declares.

    This is the single rule set behind both argument paths: the command-prefix
    parser (``!tool 30``) and the model's decoded JSON arguments
    (``{"max_results": "30"}``). Keeping one implementation means the two
    cannot disagree about what ``"30"``, ``"true"`` or ``"1.5"`` mean.

    ``"string"``, ``"object"`` and ``"array"`` parameters pass through
    unchanged — the SDK does not re-parse or re-shape structured values.

    ``None`` is a value like any other: for a numeric or boolean parameter it
    is accepted only when ``nullable`` says the parameter admits it. A JSON
    ``null`` for a non-nullable ``count: int`` reaches the method as ``None``
    and reproduces issue #78's failure one line later (``min(None, 100)``
    raises the same ``TypeError`` as ``min("30", 100)`` did), so it is refused
    here. ``nullable`` defaults to ``False`` to match
    :attr:`ToolParameter.nullable`: a caller that does not know a parameter's
    nullability gets the conservative answer.

    Args:
        value: The raw value, either a command-line token or a decoded JSON value
        param_type: The expected type from the parameter schema
        nullable: Whether the parameter accepts ``None``

    Returns:
        The coerced value, or :data:`COERCION_FAILED` when ``value`` cannot be
        represented as ``param_type``.
    """
    if value is None:
        if nullable or param_type not in _COERCED_TYPES:
            return None
        return COERCION_FAILED

    if param_type == "integer":
        # bool is a subclass of int, but a JSON ``true`` for an integer
        # parameter is a model mistake worth surfacing, not a silent 1.
        if isinstance(value, bool):
            return COERCION_FAILED
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else COERCION_FAILED
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return COERCION_FAILED
        return COERCION_FAILED

    if param_type == "number":
        if isinstance(value, bool):
            return COERCION_FAILED
        if isinstance(value, (int, float)):
            # A JSON integer is unbounded; ``float`` raises OverflowError past
            # ~1.8e308, and that must be a coercion failure, not an exception
            # escaping the tool's failure envelope (review on #78).
            try:
                converted = float(value)
            except OverflowError:
                return COERCION_FAILED
        elif isinstance(value, str):
            try:
                converted = float(value)
            except ValueError:
                return COERCION_FAILED
        else:
            return COERCION_FAILED
        # ``float`` accepts "nan", "inf" and overflowing literals like "1e309"
        # and returns a non-finite value. JSON has no such number, so it can
        # neither have been meant by the model nor be serialized back in the
        # result envelope: fail the coercion rather than pass it on.
        if not math.isfinite(converted):
            return COERCION_FAILED
        return converted

    if param_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            # JSON ``1``/``0`` mirrors the "1"/"0" tokens accepted below.
            return bool(value) if value in (0, 1) else COERCION_FAILED
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _TRUE_TOKENS:
                return True
            if token in _FALSE_TOKENS:
                return False
        return COERCION_FAILED

    return value


class ToolCategory(Enum):
    """Categories of agent tools for organization and filtering."""
    MODEL_MANAGEMENT = "model_management"
    FILE_OPERATIONS = "file_operations"
    WEB_SEARCH = "web_search"
    MEMORY = "memory"
    COMMUNICATION = "communication"
    SYSTEM = "system"
    DATA_ACCESS = "data_access"
    COMPUTE = "compute"
    UTILITY = "utility"  # Task monitoring, status checks, background operations
    AGENT_MANAGEMENT = "agent_management"  # Spawning, delegating, terminating child agents


@dataclass
class ToolParameter:
    """Definition of a tool parameter."""
    name: str
    type: str  # "string", "integer", "boolean", "array", "object"
    description: str
    required: bool = False
    default: Any = None
    enum: Optional[List[str]] = None
    items: Optional[Dict[str, Any]] = None  # JSON Schema for array element type
    # Whether the parameter's signature admits ``None`` (``Optional[int]``,
    # ``int | None``, or a literal ``None`` default). Argument coercion reads
    # this to decide whether a JSON ``null`` is a value or a type error; it is
    # deliberately last so existing positional constructions keep working, and
    # it does not change the schema advertised to the model.
    nullable: bool = False


@dataclass
class ToolSchema:
    """
    Schema defining a tool's capabilities and interface.

    This schema is used by the agent to understand when and how to use a tool,
    enabling autonomous tool selection based on context.
    """
    name: str
    description: str
    category: ToolCategory
    parameters: List[ToolParameter] = field(default_factory=list)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    command_prefix: Optional[str] = None  # e.g., "!list-models"
    is_concurrency_safe: bool = False  # True = read-only, safe to run in parallel
    # Hosts omit hidden tools from model schemas while keeping their governed
    # registrations available to other admitted callers. Exposure is not a grant.
    model_visible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model_visible, bool):
            raise TypeError("model_visible must be a boolean")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a descriptor, including host model-exposure metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": p.enum
                }
                for p in self.parameters
            ],
            "examples": self.examples,
            "command_prefix": self.command_prefix,
            "model_visible": self.model_visible,
        }

    def to_openai_format(self) -> Dict[str, Any]:
        """
        Convert schema to OpenAI function calling format.

        Returns a tool definition compatible with OpenAI's chat completions API
        tools parameter format.
        """
        # Build properties dict for parameters
        properties = {}
        required = []

        for param in self.parameters:
            prop_def: Dict[str, Any] = {
                "type": param.type,
                "description": param.description
            }

            # Add items schema for array types
            if param.type == "array":
                if param.items:
                    prop_def["items"] = param.items
                else:
                    prop_def["items"] = {"type": "object"}

            # Add enum if present
            if param.enum:
                prop_def["enum"] = param.enum

            # Add default if present and not required
            if param.default is not None and not param.required:
                prop_def["default"] = param.default

            properties[param.name] = prop_def

            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        }


class CommandParseError(ValueError):
    """A ``!command`` line could not be bound to its tool's parameters.

    Raised by :meth:`AgentTool.parse_command_args`. The message is written for
    whoever typed the command: it names the command and the actual problem
    with the input (an unbalanced quote, more values than parameters, a
    parameter given twice), so a caller can show ``str(error)`` as the
    command's failure.
    """

    def __init__(self, command: str, message: str):
        self.command = command
        super().__init__(f"{command}: {message}")


# A tuple, not a string, so the empty slice past the end of a line never
# counts as a quote (``"" in "abc"`` is true).
_QUOTES = ('"', "'")
# The ``name=`` head of a token that may bind by name. Whether it does is the
# binder's decision: only an exact declared parameter name binds.
_NAMED_TOKEN = re.compile(r"([^\W\d]\w*)=")
# How much of a user-typed value an error message echoes back.
_ECHO_LIMIT = 60


@dataclass(frozen=True)
class _CommandToken:
    """One argument of a command line.

    ``text`` is the token as a positional value, with quotes removed. A bare
    ``name=value`` token also carries ``name`` and ``value``; a quoted token
    never does, so quoting is how a literal ``a=b`` is passed positionally.
    """

    text: str
    name: Optional[str] = None
    value: Optional[str] = None


def _echo(text: str) -> str:
    rendered = repr(text)
    if len(rendered) > _ECHO_LIMIT:
        rendered = f"{rendered[:_ECHO_LIMIT]}..."
    return rendered


def _read_quoted(line: str, start: int, command: str) -> tuple[str, int]:
    """Read the quoted value opening at ``line[start]``.

    Returns the value and the index just past its closing quote.
    """
    quote = line[start]
    chars: List[str] = []
    index = start + 1
    while index < len(line):
        char = line[index]
        if char == quote:
            end = index + 1
            if end < len(line) and not line[end].isspace():
                raise CommandParseError(
                    command,
                    f"unexpected {_echo(line[end])} after the closing {quote} "
                    f"of {_echo(''.join(chars))}; a quoted value ends at its "
                    "closing quote, so to pass a value containing spaces or "
                    "quotes, quote the whole value (single quotes keep "
                    "double quotes inside it)",
                )
            return "".join(chars), end
        if quote == '"' and char == "\\" and line[index + 1:index + 2] in ('"', "\\"):
            chars.append(line[index + 1])
            index += 2
            continue
        chars.append(char)
        index += 1
    raise CommandParseError(
        command,
        f"unbalanced quote: the {quote} that opens {_echo(line[start:])} is "
        "never closed; a value that starts with a quote must end with the "
        "same quote",
    )


def _tokenize_command(line: str, command: str) -> List[_CommandToken]:
    """Split the arguments of a command line into tokens.

    Quotes are significant only where a value starts: at the start of a token,
    or right after the ``=`` of ``name=``. Anywhere else a quote character is
    ordinary text, so an unquoted ``don't`` or ``{"repo":"x"}`` arrives
    verbatim, as it did before quoting was supported.
    """
    tokens: List[_CommandToken] = []
    index = 0
    while True:
        while index < len(line) and line[index].isspace():
            index += 1
        if index == len(line):
            return tokens
        if line[index] in _QUOTES:
            value, index = _read_quoted(line, index, command)
            tokens.append(_CommandToken(text=value))
            continue
        named = _NAMED_TOKEN.match(line, index)
        if named and line[named.end():named.end() + 1] in _QUOTES:
            value, index = _read_quoted(line, named.end(), command)
            tokens.append(_CommandToken(
                text=f"{named.group(1)}={value}", name=named.group(1), value=value
            ))
            continue
        end = index
        while end < len(line) and not line[end].isspace():
            end += 1
        if named:
            tokens.append(_CommandToken(
                text=line[index:end],
                name=named.group(1),
                value=line[named.end():end],
            ))
        else:
            tokens.append(_CommandToken(text=line[index:end]))
        index = end


class AgentTool(ABC):
    """
    Base class for all agent tools.

    Tools are capabilities that agents can autonomously select and execute
    based on context, user requests, or their own decision-making.

    Example:
        class MyTool(AgentTool):
            @property
            def name(self) -> str:
                return "my_tool"

            @property
            def schema(self) -> ToolSchema:
                return ToolSchema(
                    name="my_tool",
                    description="Does something useful",
                    category=ToolCategory.SYSTEM,
                    parameters=[
                        ToolParameter(name="arg1", type="string", description="First arg", required=True)
                    ]
                )

            async def execute(self, **kwargs) -> Dict[str, Any]:
                result = await self._do_work(kwargs["arg1"])
                return {"success": True, "result": result}
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this tool."""
        pass

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """Schema defining this tool's capabilities and interface."""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool parameters as defined in schema

        Returns:
            Dict with at minimum:
                - success: bool indicating if execution succeeded
                - result: Any data returned by the tool
                - error: Optional error message if success=False
        """
        pass

    def can_handle_command(self, user_input: str) -> bool:
        """
        Check if this tool can handle a user command.

        Args:
            user_input: Raw user input string

        Returns:
            True if this tool should handle the command
        """
        if not self.schema.command_prefix:
            return False

        return user_input.strip().startswith(self.schema.command_prefix)

    def parse_command_args(self, user_input: str) -> Dict[str, Any]:
        """
        Bind a ``!command`` line to this tool's parameters.

        The text after the command prefix is split on whitespace into values,
        with quoting:

        * A value that starts with ``"`` or ``'`` runs to the matching closing
          quote and may contain whitespace; the quotes are removed. Inside
          double quotes, ``\\"`` and ``\\\\`` stand for a quote and a
          backslash; single quotes keep everything literally, which makes them
          the way to pass JSON (``'{"intent": "x y"}'``).
        * ``name=value`` binds by name when ``name`` is exactly a declared
          parameter name, and its value may be quoted
          (``query="context window"``). Any other ``a=b`` word, and any quoted
          token, is a positional value, so free text is never mistaken for a
          name.
        * A quote character inside an unquoted word is ordinary text:
          ``don't`` and ``{"repo":"x"}`` arrive verbatim.

        Positional values bind in declaration order, one value per parameter.
        No parameter takes the remaining words, so a value with spaces must be
        quoted or given as ``name=value``. Positional values come first, then
        ``name=value`` arguments.

        Each value is converted to its parameter's declared type. An
        ``object`` or ``array`` value must be JSON of that shape (``null`` too,
        when the parameter is nullable). An integer, number or boolean value
        that does not convert is passed on as the typed string, leaving the
        rejection to the tool's argument check (the ``@tool`` wrapper names
        the parameter and the type it expects). A parameter that is not given
        takes its default, unless that default is ``None``.

        Returns:
            The bound arguments, or ``{}`` when ``user_input`` is not a ``!``
            command.

        Raises:
            CommandParseError: For an unbalanced quote, text directly after a
                closing quote, more positional values than parameters, a
                positional value after a ``name=value`` argument, a parameter
                given twice, or an ``object``/``array`` value that is not JSON
                of that shape. The message names the command and the problem
                in the input, never a parameter the input did not reach.
        """
        line = user_input.strip()
        if not line.startswith("!"):
            return {}

        schema = self.schema
        # The prefix may be several words (e.g. "!memory search"). Splitting
        # off only those words keeps the arguments' own whitespace intact for
        # quoted values.
        command = schema.command_prefix or line.split()[0]
        prefix_word_count = len(command.split())
        words = line.split(maxsplit=prefix_word_count)
        arguments = words[prefix_word_count] if len(words) > prefix_word_count else ""

        parameters = {param.name: param for param in schema.parameters}
        positional: List[str] = []
        named: List[_CommandToken] = []
        for token in _tokenize_command(arguments, command):
            if token.name in parameters:
                named.append(token)
            elif named:
                raise CommandParseError(
                    command,
                    f"positional value {_echo(token.text)} follows a "
                    "name=value argument; give positional values first, "
                    "then name=value",
                )
            else:
                positional.append(token.text)

        if len(positional) > len(parameters):
            if parameters:
                plural = "" if len(parameters) == 1 else "s"
                takes = (
                    f"it takes at most {len(parameters)} positional "
                    f"value{plural} ({', '.join(parameters)})"
                )
                hint = "; quote multi-word values or use name=value"
            else:
                takes, hint = "it takes no arguments", ""
            raise CommandParseError(
                command,
                f"too many arguments: {takes} but got {len(positional)} "
                f"({', '.join(_echo(text) for text in positional)}){hint}",
            )

        supplied: Dict[str, str] = {}
        given_as: Dict[str, str] = {}
        for name, text in zip(parameters, positional):
            supplied[name] = text
            given_as[name] = f"positionally as {_echo(text)}"
        for token in named:
            again = f"as {token.name}={_echo(token.value)}"
            if token.name in supplied:
                raise CommandParseError(
                    command,
                    f"parameter '{token.name}' is given twice "
                    f"({given_as[token.name]} and {again}); give it once",
                )
            supplied[token.name] = token.value
            given_as[token.name] = again

        args: Dict[str, Any] = {}
        for name, param in parameters.items():
            if name in supplied:
                args[name] = self._convert_command_value(param, supplied[name], command)
            elif param.default is not None:
                args[name] = param.default
        return args

    def _convert_command_value(self, param: ToolParameter, text: str, command: str) -> Any:
        """Convert one command-line value to its parameter's declared type."""
        if param.type not in ("object", "array"):
            return self._coerce_type(text, param.type)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = COERCION_FAILED
        if value is None and param.nullable:
            return None
        if not isinstance(value, dict if param.type == "object" else list):
            raise CommandParseError(
                command,
                f"parameter '{param.name}' expects a JSON {param.type}, but "
                f"got {_echo(text)}; quote JSON that contains spaces, e.g. "
                f"{param.name}='...'",
            )
        return value

    def _coerce_type(self, value: str, param_type: str) -> Any:
        """
        Coerce a string value to the expected type based on schema.

        Delegates to :func:`coerce_json_value` so this path and the
        JSON-argument path share one rule set. An uncoercible token keeps this
        path's lenient behaviour and is handed back as the original string.

        Args:
            value: The string value from command parsing
            param_type: The expected type from parameter schema

        Returns:
            The coerced value, or original string if coercion fails
        """
        coerced = coerce_json_value(value, param_type)
        return value if coerced is COERCION_FAILED else coerced

    def validate_parameters(self, **kwargs) -> tuple[bool, Optional[str]]:
        """
        Validate parameters against schema.

        Args:
            **kwargs: Parameters to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        for param in self.schema.parameters:
            if param.required and param.name not in kwargs:
                return False, f"Required parameter '{param.name}' is missing"

            if param.name in kwargs:
                value = kwargs[param.name]

                # Type validation
                expected_type = param.type
                if expected_type == "string" and not isinstance(value, str):
                    return False, f"Parameter '{param.name}' must be a string"
                elif expected_type == "integer" and not isinstance(value, int):
                    return False, f"Parameter '{param.name}' must be an integer"
                elif expected_type == "boolean" and not isinstance(value, bool):
                    return False, f"Parameter '{param.name}' must be a boolean"

                # Enum validation
                if param.enum and value not in param.enum:
                    return False, f"Parameter '{param.name}' must be one of: {', '.join(param.enum)}"

        return True, None


class ToolExecutionError(Exception):
    """Raised when a tool execution fails."""

    def __init__(self, tool_name: str, message: str, original_error: Optional[Exception] = None):
        self.tool_name = tool_name
        self.original_error = original_error
        super().__init__(f"Tool '{tool_name}' failed: {message}")
