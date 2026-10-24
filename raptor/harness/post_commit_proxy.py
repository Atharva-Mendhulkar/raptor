"""
Pure Python post-commit fault proxy using stdlib asyncio.
Fulfills Fault F1 without Toxiproxy: forwards tool call, waits for upstream
commit/response, then immediately cuts client socket before response delivery.
"""
import asyncio
from typing import Optional


class PostCommitFaultProxy:
    def __init__(self, listen_port: int, upstream_port: int):
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self.drop_active = False
        self._server: Optional[asyncio.Server] = None
        self.intercepted_count = 0

    async def _handle_connection(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        except Exception:
            client_writer.close()
            return

        try:
            # Read client HTTP request and forward upstream
            req_data = await client_reader.read(65536)
            if not req_data:
                return
            up_writer.write(req_data)
            await up_writer.drain()

            # Wait for upstream response (commits effect on server first)
            resp_data = await up_reader.read(65536)
            
            if self.drop_active and b"tools/call" in req_data:
                # Fault F1: Effect committed, cut connection immediately
                self.intercepted_count += 1
                client_writer.close()
                await client_writer.wait_closed()
            else:
                client_writer.write(resp_data)
                await client_writer.drain()
                client_writer.close()
                await client_writer.wait_closed()
        except Exception:
            pass
        finally:
            up_writer.close()

    async def start(self):
        self._server = await asyncio.start_server(self._handle_connection, "127.0.0.1", self.listen_port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
