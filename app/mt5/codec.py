"""
JSON wire format shared by the MT5 bridge (Windows) and the remote client (Render).

The `MetaTrader5` package returns namedtuples, tuples, numpy structured arrays
and plain numbers, and takes datetimes and dicts as arguments. None of that is
JSON, so both sides use this module to turn values into tagged JSON and back
again. Nothing here uses pickle: decoding untrusted input can only ever produce
plain data (never execute code).

Leaf module on purpose: it imports nothing from the rest of `app`, so the
bridge can use it without loading the backend's settings.

Tags (the "__t" key):
  nt    namedtuple     {"__t":"nt","n":"TradePosition","f":{"ticket":1,...}}
  tup   plain tuple    {"__t":"tup","v":[...]}
  dict  dict           {"__t":"dict","v":{...}}
  rec   numpy structured array (candles/ticks)
                       {"__t":"rec","d":[["time","<i8"],...],"r":[[...],...]}
  nd    plain numpy array {"__t":"nd","d":"<f8","v":[...]}
  dt    datetime       {"__t":"dt","v":"2026-11-12T10:00:00"}   (naive/aware kept)
  f     non-finite float {"__t":"f","v":"nan"|"inf"|"-inf"}
Lists, strings, bools, ints, finite floats and None are sent as-is.
"""
from __future__ import annotations

import math
from collections import namedtuple
from datetime import datetime
from types import SimpleNamespace
from typing import Any

try:  # numpy is only needed for candle/tick arrays; every other value works without it
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

_NT_CACHE: dict[tuple[str, tuple[str, ...]], type] = {}


class CodecError(ValueError):
    """The payload isn't something this codec can encode/decode."""


def encode(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return {"__t": "f", "v": "nan" if math.isnan(obj) else ("inf" if obj > 0 else "-inf")}
    if np is not None and isinstance(obj, np.generic):
        return encode(obj.item())
    if np is not None and isinstance(obj, np.ndarray):
        if obj.dtype.names:
            return {"__t": "rec", "d": [[n, obj.dtype[n].str] for n in obj.dtype.names],
                    "r": [[encode(v) for v in row] for row in obj.tolist()]}
        return {"__t": "nd", "d": obj.dtype.str, "v": encode(obj.tolist())}
    if isinstance(obj, datetime):
        return {"__t": "dt", "v": obj.isoformat()}
    if isinstance(obj, tuple) and hasattr(obj, "_asdict"):
        return {"__t": "nt", "n": type(obj).__name__, "f": {k: encode(v) for k, v in obj._asdict().items()}}
    if isinstance(obj, SimpleNamespace):  # lets test fakes stand in for MT5's namedtuples
        return {"__t": "nt", "n": "Namespace", "f": {k: encode(v) for k, v in vars(obj).items()}}
    if isinstance(obj, tuple):
        return {"__t": "tup", "v": [encode(v) for v in obj]}
    if isinstance(obj, list):
        return [encode(v) for v in obj]
    if isinstance(obj, dict):
        return {"__t": "dict", "v": {str(k): encode(v) for k, v in obj.items()}}
    raise CodecError(f"Can't encode a value of type {type(obj).__name__}")


def _namedtuple(name: str, fields: tuple[str, ...]) -> type:
    key = (name, fields)
    cls = _NT_CACHE.get(key)
    if cls is None:
        cls = namedtuple(name if name.isidentifier() else "MT5Record", fields, rename=True)  # type: ignore[misc]
        _NT_CACHE[key] = cls
    return cls


def decode(obj: Any) -> Any:
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    tag = obj.get("__t")
    if tag == "nt":
        fields = obj["f"]
        return _namedtuple(str(obj.get("n", "MT5Record")), tuple(fields))(*[decode(v) for v in fields.values()])
    if tag == "tup":
        return tuple(decode(v) for v in obj["v"])
    if tag == "dict":
        return {k: decode(v) for k, v in obj["v"].items()}
    if tag == "dt":
        return datetime.fromisoformat(obj["v"])
    if tag == "f":
        return float(obj["v"])
    if tag == "rec":
        if np is None:
            raise CodecError("numpy is required to decode candle/tick arrays")
        dtype = [(str(n), str(s)) for n, s in obj["d"]]
        return np.array([tuple(decode(v) for v in row) for row in obj["r"]], dtype=dtype)
    if tag == "nd":
        if np is None:
            raise CodecError("numpy is required to decode arrays")
        return np.array(decode(obj["v"]), dtype=obj["d"])
    raise CodecError(f"Unknown or missing type tag: {tag!r}")
