"""
Загрузка аккаунтов из accounts.txt
"""
from __future__ import annotations

from models import AccountInfo


def load_accounts(file_path: str | None = None) -> list[AccountInfo]:
    if file_path is None:
        import config as cfg
        file_path = cfg.ACCOUNTS_FILE
    """
    Формат: api_key,predict_account_address,privy_wallet_private_key,proxy
    """
    accounts: list[AccountInfo] = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    continue
                if not parts[1].startswith("0x"):
                    continue
                accounts.append(AccountInfo(
                    api_key=parts[0],
                    predict_account_address=parts[1],
                    privy_wallet_private_key=parts[2],
                    proxy=parts[3] if len(parts) > 3 and parts[3] else None,
                ))
    except FileNotFoundError:
        pass
    return accounts
