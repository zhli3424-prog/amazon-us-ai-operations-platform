from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

import httpx


SYNC_TYPES = {"orders", "inventory", "tickets", "feedback", "finance", "market"}


class ChannelAdapter(Protocol):
    mode: str

    def fetch(self, sync_type: str) -> list[dict[str, Any]]: ...
    def iter_pages(self, sync_type: str, cursor: str | None = None) -> Iterator[tuple[list[dict[str, Any]], str | None, str | None]]: ...


@dataclass(frozen=True)
class DemoAmazonAdapter:
    loader: Callable[[str], list[dict[str, Any]]]
    mode: str = "demo_sp_api"

    def fetch(self, sync_type: str) -> list[dict[str, Any]]:
        if sync_type not in SYNC_TYPES:
            raise ValueError("不支持的渠道同步类型")
        return self.loader(sync_type)

    def iter_pages(self, sync_type: str, cursor: str | None = None) -> Iterator[tuple[list[dict[str, Any]], str | None, str | None]]:
        yield self.fetch(sync_type), None, None


@dataclass(frozen=True)
class HttpAmazonAdapter:
    """Adapter for an authorized SP-API gateway; credentials stay outside this app."""

    base_url: str
    token: str
    timeout_seconds: float = 30
    mode: str = "amazon_sp_api_gateway"

    def fetch_page(self, sync_type: str, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None, str | None]:
        if sync_type not in SYNC_TYPES:
            raise ValueError("不支持的渠道同步类型")
        response = httpx.get(
            f"{self.base_url.rstrip('/')}/sync/{sync_type}",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"cursor": cursor} if cursor else None,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("渠道网关必须返回对象数组 rows")
        next_cursor = payload.get("next_cursor")
        watermark = payload.get("watermark")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("渠道网关 next_cursor 必须是字符串或空值")
        return rows, next_cursor, str(watermark) if watermark is not None else None

    def fetch(self, sync_type: str) -> list[dict[str, Any]]:
        return self.fetch_page(sync_type)[0]

    def iter_pages(self, sync_type: str, cursor: str | None = None) -> Iterator[tuple[list[dict[str, Any]], str | None, str | None]]:
        seen: set[str] = set()
        for _ in range(1000):
            rows, next_cursor, watermark = self.fetch_page(sync_type, cursor)
            yield rows, next_cursor, watermark
            if not next_cursor:
                return
            if next_cursor in seen:
                raise ValueError("渠道网关返回了循环游标")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError("渠道同步分页超过安全上限")
