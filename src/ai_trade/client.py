"""Shioaji API client wrapper."""

from __future__ import annotations

import os
import shioaji as sj
from dotenv import load_dotenv

load_dotenv()


class ShioajiClient:
    """Context-manager-friendly Shioaji client.

    Usage:
        with ShioajiClient(simulation=True) as client:
            stock = client.api.Contracts.Stocks["2330"]
    """

    def __init__(self, simulation: bool = True) -> None:
        self.api = sj.Shioaji(simulation=simulation)
        self._connected = False

    def login(self) -> list:
        accounts = self.api.login(
            api_key=os.environ["API_KEY"],
            secret_key=os.environ["SECRET_KEY"],
            fetch_contract=False,
        )
        self.api.activate_ca(
            ca_path=os.environ["CA_CERT_PATH"],
            ca_passwd=os.environ["CA_PASSWORD"],
        )
        self.api.set_default_account(accounts[0])
        self._connected = True
        return accounts

    def logout(self) -> None:
        if self._connected:
            self.api.logout()
            self._connected = False

    def __enter__(self) -> ShioajiClient:
        self.login()
        return self

    def __exit__(self, *args: object) -> None:
        self.logout()
