import unittest
from types import SimpleNamespace

from bot.handlers.admin import _ack_callback_fast
from bot.handlers.membership import _ack_security_callback


class _TelegramMethodLike:
    """Awaitable-but-not-coroutine, like aiogram TelegramMethod shortcuts."""

    def __init__(self, sink: list[tuple[str, bool]], text: str, show_alert: bool) -> None:
        self._sink = sink
        self._text = text
        self._show_alert = show_alert

    def __await__(self):
        self._sink.append((self._text, self._show_alert))
        return
        yield


class _ShortcutCallback:
    def __init__(self) -> None:
        self.answered: list[tuple[str, bool]] = []

    def answer(self, text: str, show_alert: bool = False) -> _TelegramMethodLike:
        return _TelegramMethodLike(self.answered, text, show_alert)


class CallbackAckTests(unittest.IsolatedAsyncioTestCase):
    """CallbackQuery.answer() returns a TelegramMethod, not a coroutine.

    Feeding it straight into asyncio.create_task raised
    'TypeError: a coroutine was expected' and failed every security/privileged
    callback update; the ack helpers must wrap it in a real coroutine.
    """

    async def test_security_ack_accepts_telegram_method_shortcut(self) -> None:
        callback = _ShortcutCallback()
        await _ack_security_callback(callback, "正在验证权限并执行…")
        self.assertEqual(callback.answered, [("正在验证权限并执行…", False)])

    async def test_security_ack_passes_show_alert(self) -> None:
        callback = _ShortcutCallback()
        await _ack_security_callback(callback, "已拒绝", show_alert=True)
        self.assertEqual(callback.answered, [("已拒绝", True)])

    async def test_privileged_ack_accepts_telegram_method_shortcut(self) -> None:
        callback = _ShortcutCallback()
        await _ack_callback_fast(callback, "正在执行…")
        self.assertEqual(callback.answered, [("正在执行…", False)])


if __name__ == "__main__":
    unittest.main()
