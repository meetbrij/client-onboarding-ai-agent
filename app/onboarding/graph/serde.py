"""Checkpoint serializer that allows exactly this project's state models (strict msgpack).

A tampered checkpoint row cannot make LangGraph instantiate arbitrary types: only classes defined in
`onboarding.models` are allowed to be deserialised.
"""

from __future__ import annotations

import inspect

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from onboarding import models


def state_model_names() -> list[tuple[str, str]]:
    return [
        (models.__name__, name)
        for name, obj in inspect.getmembers(models, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == models.__name__
    ]


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=state_model_names())
