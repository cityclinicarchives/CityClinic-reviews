from __future__ import annotations

import json
from typing import Any

import httpx


class UmnicoError(RuntimeError):
    pass


class UmnicoClient:
    def __init__(self, api_token: str, base_url: str = "https://api.umnico.com/v1.3") -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise UmnicoError(f"Umnico API error {response.status_code}: {response.text}")
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    async def list_integrations(self) -> Any:
        return await self._request("GET", "integrations")

    async def create_webhook(self, url: str, name: str = "CityClinic feedback bot") -> Any:
        return await self._request("POST", "webhooks", json={"url": url, "name": name})

    async def list_webhooks(self) -> Any:
        return await self._request("GET", "webhooks")

    async def check_contact(self, sa_id: int, phone: str) -> bool:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/messaging/check-contact",
                headers=self.headers,
                json={"saId": sa_id, "chatId": phone},
            )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise UmnicoError(f"Umnico check-contact error {response.status_code}: {response.text}")

    async def write_first_text(self, destination: str, text: str, sa_id: int, custom_id: str | None = None) -> Any:
        payload: dict[str, Any] = {
            "message": {"text": text},
            "destination": destination,
            "saId": sa_id,
        }
        if custom_id:
            payload["customId"] = custom_id
        return await self._request("POST", "messaging/post", json=payload)

    async def send_lead_text(
        self,
        lead_id: int,
        text: str,
        source_id: str,
        user_id: int,
        custom_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "message": {"text": text},
            "source": str(source_id),
            "userId": user_id,
        }
        if custom_id:
            payload["customId"] = custom_id
        return await self._request("POST", f"messaging/{lead_id}/send", json=payload)


def extract_lead_id(value: Any) -> int | None:
    """Пытается найти leadId/id лида в разных вариантах ответа Umnico."""
    if isinstance(value, dict):
        for key in ("leadId", "lead_id"):
            if key in value and isinstance(value[key], int):
                return int(value[key])
            if key in value and isinstance(value[key], str) and value[key].isdigit():
                return int(value[key])
        lead = value.get("lead")
        if isinstance(lead, dict) and isinstance(lead.get("id"), int):
            return int(lead["id"])
        for nested in value.values():
            result = extract_lead_id(nested)
            if result:
                return result
    elif isinstance(value, list):
        for item in value:
            result = extract_lead_id(item)
            if result:
                return result
    return None
