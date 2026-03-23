"""
Аутентификация Predict Fun API (async)
"""
from __future__ import annotations

import asyncio
from config import API_BASE_URL, format_proxy


def get_auth_headers(jwt_token: str, api_key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Authorization": f"Bearer {jwt_token}",
    }


async def get_auth_jwt(
    api_key: str,
    predict_account_address: str,
    privy_wallet_private_key: str,
    proxy: str | None = None,
    log_func=print,
) -> str:
    """Получает JWT токен через подпись сообщения predict_sdk."""

    def _sync_auth() -> str:
        import requests
        from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions

        privy_key = privy_wallet_private_key
        if privy_key.startswith("0x"):
            privy_key = privy_key[2:]

        builder = OrderBuilder.make(
            ChainId.BNB_MAINNET,
            privy_key,
            OrderBuilderOptions(predict_account=predict_account_address),
        )
        proxies = format_proxy(proxy)

        msg_resp = requests.get(
            f"{API_BASE_URL}/v1/auth/message",
            headers={"x-api-key": api_key},
            proxies=proxies,
            timeout=15,
        )
        if not msg_resp.ok:
            raise RuntimeError(f"Ошибка получения сообщения: {msg_resp.status_code}")

        message = msg_resp.json()["data"]["message"]
        signature = builder.sign_predict_account_message(message)

        jwt_resp = requests.post(
            f"{API_BASE_URL}/v1/auth",
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            json={"signer": predict_account_address, "message": message, "signature": signature},
            proxies=proxies,
            timeout=15,
        )
        if not jwt_resp.ok:
            raise RuntimeError(f"Ошибка JWT: {jwt_resp.status_code}")

        return jwt_resp.json()["data"]["token"]

    try:
        jwt = await asyncio.to_thread(_sync_auth)
        log_func("✓ Аутентификация успешна")
        return jwt
    except Exception as e:
        log_func(f"✗ Ошибка аутентификации: {e}")
        raise
