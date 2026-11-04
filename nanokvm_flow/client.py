"""Native NanoKVM MCP transport; authentication never enters run artifacts."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path


def connection(config):
    provider = config.get('connection_provider')
    if provider:
        path, function = provider.rsplit(':', 1)
        spec = importlib.util.spec_from_file_location('_nanokvm_connection', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, function)()
    return config['mcp_url'], os.environ['NANOKVM_API_KEY']


class NativeMCP:
    def __init__(self, config):
        self.config = config
        self.session = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        url, key = connection(self.config)
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        try:
            http = create_mcp_http_client(headers={'Authorization': 'Bearer ' + key})
            await self.stack.enter_async_context(http)
            read, write = await self.stack.enter_async_context(streamable_http_client(url, http_client=http))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        await self.stack.aclose()

    async def call(self, tool, arguments):
        result = await self.session.call_tool(tool, arguments)
        if getattr(result, 'is_error', getattr(result, 'isError', False)):
            raise RuntimeError(tool + ' returned an error')
        return result

    async def keys(self, keys, hold_ms=50):
        return await self.call('cube_press_keys', {'keys': keys, 'holdMs': hold_ms})

    async def text(self, text):
        result = await self.call('cube_type_text', {'text': text, 'delayMs': 30})
        for block in result.content:
            if getattr(block, 'type', '') == 'text':
                try:
                    if json.loads(block.text).get('skippedCount', 0):
                        raise RuntimeError('NanoKVM skipped characters')
                except json.JSONDecodeError:
                    pass
        return result

    async def _screenshot_once(self):
        # The device exposes no HDMI-status MCP tool: a successful captured image
        # is the positive video observation. Auth/transport errors are NOT no-signal.
        result = await self.session.call_tool('cube_screenshot', {'quality': 60, 'timeoutMs': 1500})
        metadata = getattr(result, 'structured_content', None) or getattr(result, 'structuredContent', None) or {}
        picture = None
        for block in result.content:
            if getattr(block, 'type', '') == 'image':
                picture = base64.b64decode(block.data)
            elif getattr(block, 'type', '') == 'text':
                try:
                    item = json.loads(block.text)
                    if isinstance(item, dict):
                        metadata.update(item)
                except json.JSONDecodeError:
                    pass
        return picture, metadata

    async def screenshot(self):
        # Observed on this device: the first capture after HID can be the frame
        # preceding the last capture, even after a 2-second wait. Flush that
        # buffered frame and keep the second native capture, not a blind delay.
        await self._screenshot_once()
        await asyncio.sleep(.15)
        return await self._screenshot_once()
