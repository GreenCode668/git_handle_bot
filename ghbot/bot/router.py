"""Callback-query routing and pending text-input state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update

from ghbot.bot.ui import Ctx

CallbackFn = Callable[[Update, Ctx, tuple[Any, ...]], Awaitable[None]]
InputFn = Callable[[Update, Ctx, dict[str, Any], str], Awaitable[None]]

CALLBACKS: dict[str, CallbackFn] = {}
INPUTS: dict[str, InputFn] = {}


def callback(route: str) -> Callable[[CallbackFn], CallbackFn]:
    def register(fn: CallbackFn) -> CallbackFn:
        if route in CALLBACKS:
            raise RuntimeError(f"duplicate callback route {route}")
        CALLBACKS[route] = fn
        return fn

    return register


def text_input(kind: str) -> Callable[[InputFn], InputFn]:
    def register(fn: InputFn) -> InputFn:
        INPUTS[kind] = fn
        return fn

    return register


def await_input(context: Ctx, kind: str, **data: Any) -> None:
    context.user_data["awaiting"] = {"kind": kind, **data}  # type: ignore[index]


def clear_input(context: Ctx) -> dict[str, Any] | None:
    return context.user_data.pop("awaiting", None)  # type: ignore[union-attr]
