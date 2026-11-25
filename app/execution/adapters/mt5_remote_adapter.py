"""
MT5Adapter that reaches the terminal through the MT5 bridge (MT5_BRIDGE_URL).

It IS the normal `MT5Adapter` (same order, sizing and close logic); the only
difference is that `import MetaTrader5` is replaced by `RemoteMT5`, which sends
each call to the Windows machine. Selected automatically when
BROKER_ADAPTER=mt5 and MT5_BRIDGE_URL is set. MT5Adapter itself is untouched.
"""
from app.execution.adapters.mt5_adapter import MT5Adapter
from app.mt5.remote import get_remote_mt5


class MT5RemoteAdapter(MT5Adapter):
    def __init__(self, server: str = "", symbol_suffix: str = "") -> None:
        # The login/password live on the bridge machine and never travel over the network.
        super().__init__(login=0, password="", server=server, symbol_suffix=symbol_suffix)

    def _connect_sync(self) -> None:
        remote = get_remote_mt5()
        if not remote.initialize():
            raise RuntimeError(f"MT5 bridge unavailable: {remote.last_error()[1]}")
        self._mt5 = remote
        self._connected = True
