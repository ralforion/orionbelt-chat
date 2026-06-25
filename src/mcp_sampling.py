"""Enable MCP `sampling.tools` capability in pydantic-ai's MCP client.

Pydantic-AI 1.77.0 constructs `mcp.ClientSession` without
`sampling_capabilities`, so the server side rejects sampling calls that
include tools (`mcp/server/validation.py:55`). We patch the
`ClientSession` symbol in `pydantic_ai.mcp` to a subclass that defaults
`sampling_capabilities` to advertise `sampling.tools`. Importing this
module is enough — pydantic-ai's existing call site picks up the
subclass via the module's global lookup at session-open time.
"""

import pydantic_ai.mcp as _pa_mcp
from mcp import ClientSession
from mcp.types import SamplingCapability, SamplingToolsCapability


class _SamplingToolsClientSession(ClientSession):
    """ClientSession that advertises `sampling.tools` when a callback is set.

    Servers can issue `sampling/createMessage` with a `tools` parameter
    (per the MCP spec) instead of falling back to a manual review path.
    If pydantic-ai constructs us with `sampling_callback=None` (i.e.
    `allow_sampling=False`), we keep the capability unset so we don't
    advertise something we can't fulfill.
    """

    def __init__(
        self,
        *args,
        sampling_callback=None,
        sampling_capabilities=None,
        **kwargs,
    ):
        if sampling_callback is not None and sampling_capabilities is None:
            sampling_capabilities = SamplingCapability(tools=SamplingToolsCapability())
        super().__init__(
            *args,
            sampling_callback=sampling_callback,
            sampling_capabilities=sampling_capabilities,
            **kwargs,
        )


# Intentional monkeypatch of the module's `ClientSession` symbol (a type).
_pa_mcp.ClientSession = _SamplingToolsClientSession  # type: ignore[misc]
