"""Offline regression tests: no Telegram/AI calls or saved-data writes."""

import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class StickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patcher in (
            patch.object(bot, "stickers", [{"file_id": f"sticker-{i}"} for i in range(5)]),
            patch.object(bot, "sticker_cooldown", defaultdict(int)),
            patch.object(bot.config, "ENABLE_STICKERS", True),
            patch.object(bot.config, "STICKER_COOLDOWN", 2),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_screenshot_requests_without_ai_tags_bypass_cooldown(self):
        for text, count in (("[测试] 再发一张", 1), ("[测试] 再发2张", 2), ("[测试] 连续发送3个贴纸", 3)):
            with self.subTest(text=text):
                bot.sticker_cooldown["chat"] = 3
                ids, clean = bot.extract_stickers("chat", "拿去拿去～宝宝开心就好呀", text)
                self.assertEqual(len(set(ids)), count)
                self.assertEqual(clean, "拿去拿去～宝宝开心就好呀")

    def test_quantity_classifiers_and_chinese_numbers(self):
        for text, count in (
            ("连续发送3个贴纸", 3), ("发两个贴纸", 2),
            ("再发 2 个", 2), ("给我三枚贴纸", 3),
            ("发四张贴纸", 3), ("发十一个贴纸", 3),
            ("再发99个贴纸", 3), ("发几个贴纸", 2),
            ("发多个贴纸", 2), ("发一张贴纸", 1),
            ("我有3个问题", 0),
        ):
            with self.subTest(text=text):
                self.assertEqual(bot.wanted_sticker_count(text), count)

    async def test_exact_screenshot_request_delivers_three_in_both_modes(self):
        for business in (False, True):
            for reply in ("接好喽～", "接好喽～[sticker:1]"):
                with self.subTest(business=business, reply=reply):
                    api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock())
                    target = SimpleNamespace(reply_text=AsyncMock(), reply_sticker=AsyncMock())
                    bot.sticker_cooldown["chat"] = 5
                    await bot.deliver_reply(
                        SimpleNamespace(bot=api), 1, "chat", reply,
                        bc_id="connection" if business else None,
                        target=None if business else target,
                        user_text="[测试] 连续发送3个贴纸",
                    )
                    sender = api.send_sticker if business else target.reply_sticker
                    self.assertEqual(sender.await_count, 3)
                    ids = [call.kwargs["sticker"] if business else call.args[0]
                           for call in sender.await_args_list]
                    self.assertEqual(len(set(ids)), 3)

    def test_valid_choice_preserved_and_missing_count_filled(self):
        ids, clean = bot.extract_stickers("chat", "给你～[sticker:2]", "再发三张")
        self.assertEqual(ids[0], "sticker-1")
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(clean, "给你～")

    def test_invalid_and_duplicate_tags_still_fill_requested_count(self):
        for tags in ("[sticker:999][sticker:0]", "[sticker:1][sticker:1]"):
            with self.subTest(tags=tags):
                ids, clean = bot.extract_stickers("chat", tags, "再发2张")
                self.assertEqual(len(set(ids)), 2)
                self.assertEqual(clean, "")

    def test_random_tags_and_requested_count_are_capped(self):
        ids, _ = bot.extract_stickers("chat", "[sticker:random]" * 5, "发99张")
        self.assertEqual(len(set(ids)), 3)

    def test_short_inventory_does_not_duplicate(self):
        bot.stickers[:] = [{"file_id": "one"}, {"file_id": "one"}]
        ids, _ = bot.extract_stickers("chat", "好呀", "发三张")
        self.assertEqual(ids, ["one"])

    def test_no_request_and_no_tag_does_not_send(self):
        self.assertEqual(bot.extract_stickers("chat", "你好呀", "你好"), ([], "你好呀"))

    def test_mentioning_stickers_or_other_images_is_not_a_request(self):
        for text in ("这张贴纸很好看", "我买了三张票", "再发一次地址", "发三张照片"):
            with self.subTest(text=text):
                self.assertEqual(bot.wanted_sticker_count(text), 0)

    def test_explicit_no_stickers_overrides_model_tags(self):
        for text in ("不要发贴纸", "别发了", "只回文字"):
            with self.subTest(text=text):
                ids, clean = bot.extract_stickers("chat", "好的[sticker:1]", text)
                self.assertEqual(ids, [])
                self.assertEqual(clean, "好的")

    def test_automatic_stickers_keep_cooldown_and_single_limit(self):
        bot.sticker_cooldown["chat"] = 3
        ids, _ = bot.extract_stickers("chat", "[sticker:1]", "你好")
        self.assertEqual(ids, [])
        bot.sticker_cooldown["chat"] = 0
        ids, _ = bot.extract_stickers("chat", "[sticker:1][sticker:2]", "你好")
        self.assertEqual(ids, ["sticker-0"])

    def test_disabled_or_empty_inventory_does_not_send(self):
        with patch.object(bot.config, "ENABLE_STICKERS", False):
            self.assertEqual(bot.extract_stickers("chat", "好呀[sticker:1]", "发两张"), ([], "好呀"))
        bot.stickers.clear()
        self.assertEqual(bot.extract_stickers("chat", "好呀[sticker:1]", "发两张"), ([], "好呀"))

    async def test_direct_delivery_without_tags(self):
        target = SimpleNamespace(reply_text=AsyncMock(), reply_sticker=AsyncMock())
        await bot.deliver_reply(SimpleNamespace(), 1, "chat", "好呀", target=target, user_text="再发2张")
        target.reply_text.assert_awaited_once_with("好呀")
        self.assertEqual(target.reply_sticker.await_count, 2)
        ids = [call.args[0] for call in target.reply_sticker.await_args_list]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(bot.sticker_cooldown["chat"], 2)

    async def test_echoed_internal_status_is_removed_in_both_delivery_modes(self):
        for business in (False, True):
            for marker in ("[已发送1张贴纸]", "【已发送 1 张贴纸】"):
                with self.subTest(business=business, marker=marker):
                    api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock())
                    target = SimpleNamespace(reply_text=AsyncMock(), reply_sticker=AsyncMock())
                    result = await bot.deliver_reply(
                        SimpleNamespace(bot=api), 1, "chat", f"抱抱～\n{marker}\n[sticker:1]",
                        bc_id="connection" if business else None,
                        target=None if business else target, user_text="发张贴纸",
                    )
                    if business:
                        api.send_message.assert_awaited_once_with(chat_id=1, text="抱抱～", business_connection_id="connection")
                    else:
                        target.reply_text.assert_awaited_once_with("抱抱～")
                    self.assertEqual(result.text, "抱抱～")
                    self.assertEqual(result.sent_stickers, 1)

    async def test_internal_status_alone_does_not_send_a_message_or_claim_success(self):
        target = SimpleNamespace(reply_text=AsyncMock(), reply_sticker=AsyncMock())
        result = await bot.deliver_reply(SimpleNamespace(), 1, "chat", "[已发送1张贴纸]", target=target)
        self.assertIsNone(result)
        target.reply_text.assert_not_awaited()
        target.reply_sticker.assert_not_awaited()

    async def test_automatic_stickers_resume_second_turn_even_after_three(self):
        api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock())
        context = SimpleNamespace(bot=api)
        await bot.deliver_reply(context, 1, "chat", "好呀", user_text="发三个贴纸")
        self.assertEqual(api.send_sticker.await_count, 3)
        self.assertEqual(bot.sticker_cooldown["chat"], 2)

        await bot.deliver_reply(context, 1, "chat", "嘿嘿[sticker:1]", user_text="开心")
        self.assertEqual(api.send_sticker.await_count, 3)
        await bot.deliver_reply(context, 1, "chat", "抱抱[sticker:2]", user_text="好开心")
        self.assertEqual(api.send_sticker.await_count, 4)
        self.assertEqual(bot.sticker_cooldown["chat"], 2)

    async def test_business_delivery_preserves_connection(self):
        api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock())
        await bot.deliver_reply(SimpleNamespace(bot=api), 1, "biz:1", "好呀", bc_id="connection", user_text="再发2张")
        api.send_message.assert_awaited_once_with(chat_id=1, text="好呀", business_connection_id="connection")
        self.assertEqual(api.send_sticker.await_count, 2)
        for call in api.send_sticker.await_args_list:
            self.assertEqual(call.kwargs["business_connection_id"], "connection")
            self.assertEqual(call.kwargs["chat_id"], 1)

    async def test_failed_send_continues_and_only_success_sets_cooldown(self):
        api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock(side_effect=[RuntimeError("offline"), None]))
        with self.assertLogs(bot.logger, level="INFO") as logs:
            await bot.deliver_reply(SimpleNamespace(bot=api), 1, "chat", "好呀", user_text="再发2张")
        self.assertEqual(api.send_sticker.await_count, 2)
        self.assertEqual(bot.sticker_cooldown["chat"], 2)
        self.assertTrue(any("成功 1/2 张" in entry for entry in logs.output))

    async def test_all_sends_fail_without_starting_cooldown(self):
        api = SimpleNamespace(send_message=AsyncMock(), send_sticker=AsyncMock(side_effect=RuntimeError("offline")))
        with self.assertLogs(bot.logger, level="INFO") as logs:
            await bot.deliver_reply(SimpleNamespace(bot=api), 1, "chat", "好呀", user_text="再发2张")
        self.assertEqual(bot.sticker_cooldown["chat"], 0)
        self.assertTrue(any("成功 0/2 张" in entry for entry in logs.output))


if __name__ == "__main__":
    unittest.main()
