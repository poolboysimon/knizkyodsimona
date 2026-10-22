import asyncio
import logging
import httpx

LOG = logging.getLogger(__name__)

class APIError(RuntimeError):
    pass

class ImportAPI:
    def __init__(self, base: str, token: str, timeout: float = 120):
        self.base = base.rstrip('/')
        self.http = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self.headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    async def close(self):
        await self.http.aclose()

    async def call(self, action: str, payload: dict | None = None) -> dict:
        if action not in {'heartbeat','meta','claim','files','download-urls','upload-urls','progress','fail','results'}:
            raise ValueError('Unknown action')
        for attempt in range(5):
            try:
                r = await self.http.post(f'{self.base}/{action}', json=payload or {}, headers=self.headers)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f'API {r.status_code}: {r.text[:300]}', request=r.request, response=r)
                if r.status_code >= 400:
                    raise APIError(f'{action}: HTTP {r.status_code}: {r.text[:500]}')
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 4: raise
                await asyncio.sleep(min(2**attempt, 16))
        raise RuntimeError('unreachable')

    async def download(self, url: str, dest, max_bytes: int):
        # Signed URLs are returned by the trusted API. Never log URLs.
        async with self.http.stream('GET', url, follow_redirects=False) as r:
            r.raise_for_status()
            n = 0
            with open(dest, 'wb') as f:
                async for chunk in r.aiter_bytes():
                    n += len(chunk)
                    if n > max_bytes: raise ValueError('Downloaded file exceeds size limit')
                    f.write(chunk)

    async def upload(self, url: str, src, content_type: str):
        # Signed PUT URLs. Retry safely on 5xx/network errors.
        for attempt in range(4):
            try:
                with open(src,'rb') as f:
                    r = await self.http.put(url, content=f, headers={'Content-Type': content_type})
                if r.status_code >= 500:
                    r.raise_for_status()
                if r.status_code >= 400: raise APIError(f'Upload failed HTTP {r.status_code}: {r.text[:200]}')
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3: raise
                await asyncio.sleep(2**attempt)
