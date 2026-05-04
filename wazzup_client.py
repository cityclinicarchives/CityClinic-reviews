from __future__ import annotations

import json
from typing import Any

import httpx


class WazzupError(RuntimeError):
    pass


class WazzupClient:
    def __init__(self, api_token: str, base_url: str = "https://api.wazzup24.com/v3") -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise WazzupError(f"Wazzup API error {response.status_code}: {response.text}")
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    async def list_channels(self) -> Any:
        return await self._request("GET", "channels")

    async def send_text(
        self,
        *,
        channel_id: str,
        chat_id: str,
        text: str,
        crm_message_id: str | None = None,
        clear_unanswered: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {
            "channelId": channel_id,
            "chatType": "whatsapp",
            "chatId": chat_id,
            "text": text,
            "clearUnanswered": clear_unanswered,
        }
        if crm_message_id:
            payload["crmMessageId"] = crm_message_id
        return await self._request("POST", "message", json=payload)

    async def setup_webhook(self, webhook_url: str) -> Any:
        payload = {
            "webhooksUri": webhook_url,
            "subscriptions": {
                "messagesAndStatuses": True,
                "contactsAndDealsCreation": False,
                "channelsUpdates": False,
                "templateStatus": False,
            },
        }
        return await self._request("PATCH", "webhooks", json=payload)

    async def get_webhooks(self) -> Any:
        return await self._request("GET", "webhooks")
