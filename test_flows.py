"""Offline tests for the actual test-chat and business-message pipelines."""

import asyncio
import logging
import unittest
from collections import OrderedDict, defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import Update
from telegram.error import TelegramError

import bot
from runtime_support import RedactingFormatter


def message(chat_id=10, sender_id=None, message_id=1, text="你好", business=False):
    return NS(
        chat=NS(id=chat_id, type="private"),
        from_user=NS(id=sender_id if sender_id is not None else chat_id, is_bot=False),
        message_id=message_id, text=text, caption=None, sticker=None,
        business_connection_id="connection" if business else None,
        sender_business_bot=None, reply_text=AsyncMock(), reply_sticker=AsyncMock(),
    )


class ChatFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = NS(id="connection", user=NS(id=99), is_enabled=True, rights=NS(can_reply=True))
        self.api = NS(send_chat_action=AsyncMock(), send_message=AsyncMock(), send_sticker=AsyncMock(),
                      get_business_connection=AsyncMock(return_value=self.connection))
        self.context = NS(bot=self.api)
        patches = (
            patch.object(bot, "chat_history", defaultdict(list)),
            patch.object(bot, "chat_locks", defaultdict(asyncio.Lock)),
            patch.object(bot, "chat_revisions", defaultdict(int)),
            patch.object(bot, "pending_replies", {}),
            patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0),
            patch.object(bot, "sticker_cooldown", defaultdict(int)),
            patch.object(bot, "biz_connections", {"connection": self.connection}),
            patch.object(bot, "_processed_biz_ids", OrderedDict()),
            patch.object(bot, "stickers", [{"file_id": "one", "emoji": "🥰"}]),
            patch.object(bot, "save_history"), patch.object(bot, "save_stickers"),
            patch.object(bot.config, "ENABLE_STICKERS", True),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_direct_and_business_share_engine_but_not_history(self):
        with patch.object(bot, "ask_deepseek", AsyncMock(return_value="抱抱[sticker:1]")) as ask:
            direct = message(text="[测试] 你好")
            await bot.handle_message(NS(message=direct), self.context)
            formal = message(business=True)
            await bot.handle_business_message(NS(business_message=formal), self.context)
        self.assertEqual(ask.await_args_list[0].args, ("10", "你好"))
        self.assertEqual(ask.await_args_list[1].args, ("biz:10", "你好"))
        self.assertIsNot(bot.chat_history["10"], bot.chat_history["biz:10"])
        self.assertEqual(bot.chat_history["biz:10"][-1]["content"], "抱抱")
        self.assertEqual(bot.chat_history["biz:10"][-1]["sent_stickers"], 1)
        self.api.send_sticker.assert_awaited_once_with(chat_id=10, sticker="one", business_connection_id="connection")

    async def test_same_message_number_in_different_chats_is_not_dropped(self):
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            for chat_id in (10, 20, 10):
                await bot.handle_business_message(NS(business_message=message(chat_id=chat_id, business=True)), self.context)
            self.assertEqual(reply.await_count, 2)

    async def test_owner_and_bot_echo_are_not_answered(self):
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            owner = message(sender_id=99, business=True)
            await bot.handle_business_message(NS(business_message=owner), self.context)
            echo = message(message_id=2, business=True)
            echo.sender_business_bot = NS(id=123)
            await bot.handle_business_message(NS(business_message=echo), self.context)
            reply.assert_not_awaited()
        self.assertEqual(bot.chat_history["biz:10"][0]["sender"], "owner")

    async def test_missing_sender_or_mismatched_sender_is_ignored(self):
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            for sender in (None, NS(id=88, is_bot=False)):
                msg = message(business=True)
                msg.from_user = sender
                await bot.handle_business_message(NS(business_message=msg), self.context)
            reply.assert_not_awaited()

    async def test_disabled_or_unpermitted_connection_never_replies(self):
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            for enabled, rights in ((False, NS(can_reply=True)), (True, None), (True, NS(can_reply=False))):
                self.connection.is_enabled, self.connection.rights = enabled, rights
                await bot.handle_business_message(NS(business_message=message(business=True)), self.context)
            reply.assert_not_awaited()

    async def test_connection_lookup_failure_is_closed(self):
        bot.biz_connections.clear()
        self.api.get_business_connection.side_effect = TelegramError("offline")
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply, self.assertLogs(bot.logger, "WARNING"):
            await bot.handle_business_message(NS(business_message=message(business=True)), self.context)
            reply.assert_not_awaited()

    async def test_connection_revocation_during_lookup_is_not_overwritten(self):
        bot.biz_connections.clear()

        async def lookup(_):
            bot.biz_connections["connection"] = NS(is_enabled=False)
            return self.connection

        self.api.get_business_connection.side_effect = lookup
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            await bot.handle_business_message(NS(business_message=message(business=True)), self.context)
            reply.assert_not_awaited()

    async def test_typing_failure_does_not_block_text_reply(self):
        self.api.send_chat_action.side_effect = TelegramError("typing unavailable")
        msg = message()
        with patch.object(bot, "ask_deepseek", AsyncMock(return_value="你好呀")):
            await bot.reply_to_chat(self.context, msg, "10", "你好")
        msg.reply_text.assert_awaited_once_with("你好呀")

    async def test_failed_delivery_never_commits_fake_reply(self):
        msg = message()
        msg.reply_text.side_effect = TelegramError("offline")
        with patch.object(bot, "ask_deepseek", AsyncMock(return_value="你好呀")), self.assertLogs(bot.logger, "WARNING"):
            await bot.reply_to_chat(self.context, msg, "10", "你好")
        self.assertEqual(bot.chat_history["10"], [])

    async def test_same_chat_requests_are_serialized(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def ask(key, text):
            if text == "first":
                entered.set()
                await release.wait()
            else:
                self.assertEqual(bot.chat_history[key][-1]["content"], "reply-first")
            return "reply-" + text

        with patch.object(bot, "ask_deepseek", AsyncMock(side_effect=ask)) as ask_mock:
            first = asyncio.create_task(bot.reply_to_chat(self.context, message(), "10", "first"))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(bot.reply_to_chat(self.context, message(), "10", "second"))
            await asyncio.sleep(0)
            self.assertEqual(ask_mock.await_count, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual([m["content"] for m in bot.chat_history["10"]],
                         ["first", "reply-first", "second", "reply-second"])

    async def test_different_chats_do_not_block_each_other(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def ask(key, text):
            if key == "10":
                entered.set()
                await release.wait()
            return "hello"

        with patch.object(bot, "ask_deepseek", AsyncMock(side_effect=ask)):
            first = asyncio.create_task(bot.reply_to_chat(self.context, message(), "10", "hi"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await asyncio.wait_for(bot.reply_to_chat(self.context, message(20), "20", "hi"), 1)
                self.assertFalse(first.done())
            finally:
                release.set()
                await asyncio.wait_for(first, 1)

    async def test_owner_interruption_cancels_generated_and_queued_replies(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def ask(*_):
            entered.set()
            await release.wait()
            return "stale answer"

        with patch.object(bot, "ask_deepseek", AsyncMock(side_effect=ask)) as ask_mock:
            first = asyncio.create_task(bot.reply_to_chat(self.context, message(business=True), "biz:10", "hi", "connection"))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(bot.reply_to_chat(self.context, message(business=True), "biz:10", "again", "connection"))
            await asyncio.sleep(0)
            await bot.handle_business_message(NS(business_message=message(sender_id=99, message_id=3, text="我回来了", business=True)), self.context)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
            self.assertEqual(ask_mock.await_count, 1)
        self.api.send_message.assert_not_awaited()
        self.assertEqual(bot.chat_history["biz:10"][-1]["content"], "我回来了")

    async def test_private_stickers_are_collected_then_chatted_and_deduplicated(self):
        msg = message(text=None)
        msg.sticker = NS(file_id="new-id", file_unique_id="unique", emoji="🥰", is_video=False, is_animated=False)
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            await bot.handle_sticker_received(NS(message=msg), self.context)
            msg.sticker.file_id = "refreshed-id"
            await bot.handle_sticker_received(NS(message=msg), self.context)
        self.assertEqual(len(bot.stickers), 2)
        self.assertEqual(bot.stickers[-1]["file_id"], "refreshed-id")
        self.assertEqual(reply.await_count, 2)

    async def test_business_sticker_is_answered_without_collecting(self):
        msg = message(text=None, business=True)
        msg.sticker = NS(emoji="🥰")
        with patch.object(bot, "reply_to_chat", AsyncMock()) as reply:
            await bot.handle_business_message(NS(business_message=msg), self.context)
        self.assertEqual(reply.await_args.args[3], "")
        self.assertTrue(reply.await_args.kwargs["emotion_only"])
        bot.save_stickers.assert_not_called()

    async def test_reset_only_clears_test_memory(self):
        bot.chat_history["10"] = [{"role": "user", "content": "test"}]
        bot.chat_history["biz:10"] = [{"role": "user", "content": "real"}]
        await bot.reset(NS(effective_user=NS(id=10), message=message()), self.context)
        self.assertNotIn("10", bot.chat_history)
        self.assertEqual(bot.chat_history["biz:10"][0]["content"], "real")

    async def test_delete_sticker_removes_only_replied_file_and_saves(self):
        bot.stickers.append({"file_id": "keep"})
        msg = message()
        msg.reply_to_message = NS(sticker=NS(file_id="one", file_unique_id="unique-one"))
        await bot.delete_sticker_cmd(NS(message=msg), self.context)
        self.assertEqual(bot.stickers, [{"file_id": "keep"}])
        bot.save_stickers.assert_called_once()
        self.assertIn("已删除", msg.reply_text.await_args.args[0])

    async def test_delete_sticker_matches_unique_id_when_file_id_changed(self):
        bot.stickers[0]["file_unique_id"] = "unique-one"
        msg = message()
        msg.reply_to_message = NS(sticker=NS(file_id="changed", file_unique_id="unique-one"))
        await bot.delete_sticker_cmd(NS(message=msg), self.context)
        self.assertEqual(bot.stickers, [])

    async def test_delete_sticker_requires_replied_sticker(self):
        msg = message()
        for target in (None, NS(sticker=None)):
            msg.reply_to_message = target
            await bot.delete_sticker_cmd(NS(message=msg), self.context)
        self.assertEqual(len(bot.stickers), 1)
        bot.save_stickers.assert_not_called()

    async def test_delete_unknown_sticker_does_not_change_collection(self):
        msg = message()
        msg.reply_to_message = NS(sticker=NS(file_id="unknown", file_unique_id="unknown-unique"))
        await bot.delete_sticker_cmd(NS(message=msg), self.context)
        self.assertEqual(len(bot.stickers), 1)
        bot.save_stickers.assert_not_called()

    async def test_delete_sticker_rolls_back_on_save_failure(self):
        bot.save_stickers.return_value = False
        msg = message()
        msg.reply_to_message = NS(sticker=NS(file_id="one", file_unique_id="unique-one"))
        await bot.delete_sticker_cmd(NS(message=msg), self.context)
        self.assertEqual(bot.stickers[0]["file_id"], "one")
        self.assertIn("保存失败", msg.reply_text.await_args.args[0])

    def test_history_limit_includes_delivered_answer(self):
        with patch.object(bot.config, "MAX_HISTORY", 4):
            for i in range(10):
                bot.remember("10", {"role": "assistant", "content": str(i)})
        self.assertEqual(len(bot.chat_history["10"]), 4)
        self.assertEqual(bot.chat_history["10"][-1]["content"], "9")

    def test_legacy_status_and_local_metadata_never_enter_model_dialogue(self):
        bot.chat_history["10"] = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "抱抱\n[已发送1张贴纸]", "sender": "ai"},
            {"role": "assistant", "content": "[已发送2张贴纸]", "sender": "ai"},
            {"role": "assistant", "content": "亲亲", "sent_stickers": 1, "sender": "ai"},
        ]
        messages = bot.build_messages("10", "再聊聊")
        self.assertEqual([m["content"] for m in messages[1:]], ["你好", "抱抱", "亲亲", "再聊聊"])
        self.assertTrue(all(set(m) == {"role", "content"} for m in messages))

    def test_legacy_migration_preserves_actual_owner_text(self):
        original = {"role": "assistant", "content": "抱抱\n[已发送2张贴纸]", "sender": "ai"}
        migrated = bot.normalize_history_message(original)
        self.assertEqual(migrated["content"], "抱抱")
        self.assertEqual(migrated["sent_stickers"], 2)
        self.assertIn("已发送", original["content"])
        owner = dict(original, sender="owner")
        self.assertEqual(bot.normalize_history_message(owner), owner)

    async def test_sticker_only_delivery_has_metadata_without_fake_dialogue(self):
        msg = message(text="发张贴纸")
        with patch.object(bot, "ask_deepseek", AsyncMock(return_value="[sticker:1]")):
            await bot.reply_to_chat(self.context, msg, "10", msg.text)
        msg.reply_text.assert_not_awaited()
        msg.reply_sticker.assert_awaited_once_with("one")
        stored = bot.chat_history["10"][-1]
        self.assertEqual(stored["content"], "")
        self.assertEqual(stored["sent_stickers"], 1)
        self.assertFalse(any(m["role"] == "assistant" for m in bot.build_messages("10", "你好")))

    async def test_standalone_sticker_only_returns_a_sticker_without_model_interpretation(self):
        msg = message(text=None)
        msg.sticker = NS(emoji="❤️")
        bot.sticker_cooldown["10"] = 2
        with patch.object(bot, "ask_deepseek", AsyncMock(return_value="抱抱[sticker:1]")) as ask:
            await bot.handle_message(NS(message=msg), self.context)
        ask.assert_not_awaited()
        msg.reply_text.assert_not_awaited()
        msg.reply_sticker.assert_awaited_once_with("one")

    def test_emoji_detection_keeps_words_and_numbers_as_text(self):
        for text in ("❤️", "😢", "🥰 🥰", "👨‍👩‍👧", "🇨🇳", "👍🏽"):
            self.assertTrue(bot.is_emotion_message(message(text=text), text), text)
        for text in ("今天好累😢", "123", "", "晚上见", "3个贴纸"):
            self.assertFalse(bot.is_emotion_message(message(text=text), text), text)

    async def test_standalone_emoji_uses_no_model_in_both_modes(self):
        with patch.object(bot, "ask_deepseek", AsyncMock()) as ask:
            for business in (False, True):
                msg = message(text="😢", business=business)
                if business:
                    await bot.handle_business_message(NS(business_message=msg), self.context)
                else:
                    await bot.handle_message(NS(message=msg), self.context)
                msg.reply_text.assert_not_awaited()
            ask.assert_not_awaited()
        self.api.send_message.assert_not_awaited()
        self.api.send_sticker.assert_awaited_once()
        self.assertEqual(bot.chat_history["10"][-2]["content"], "")
        self.assertTrue(bot.chat_history["10"][-2]["emotion_only"])

    async def test_text_then_sticker_is_one_text_reply_then_one_sticker_in_both_modes(self):
        for business in (False, True):
            with self.subTest(business=business):
                order = []
                key = "biz:10" if business else "10"
                bot.sticker_cooldown[key] = 5
                text_msg = message(text="今天工作好累", message_id=10, business=business)
                emotion = message(text=None, message_id=11, business=business)
                emotion.sticker = NS(emoji="😢")
                if business:
                    self.api.send_message.side_effect = lambda **kw: order.append(("text", kw["text"]))
                    self.api.send_sticker.side_effect = lambda **kw: order.append(("sticker", kw["sticker"]))
                    first = bot.handle_business_message(NS(business_message=text_msg), self.context)
                    second = bot.handle_business_message(NS(business_message=emotion), self.context)
                else:
                    text_msg.reply_text.side_effect = lambda text: order.append(("text", text))
                    text_msg.reply_sticker.side_effect = lambda sticker: order.append(("sticker", sticker))
                    first = bot.handle_message(NS(message=text_msg), self.context)
                    second = bot.handle_message(NS(message=emotion), self.context)
                with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                        patch.object(bot, "ask_deepseek", AsyncMock(return_value="辛苦啦，抱抱")) as ask:
                    await asyncio.gather(first, second)
                ask.assert_awaited_once_with(key, "今天工作好累")
                self.assertEqual(order, [("text", "辛苦啦，抱抱"), ("sticker", "one")])
                emotion.reply_text.assert_not_awaited()
                emotion.reply_sticker.assert_not_awaited()

    async def test_multiple_emotions_in_same_burst_get_only_one_sticker(self):
        msg = message(text="🥰")
        with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                patch.object(bot, "ask_deepseek", AsyncMock()) as ask:
            await asyncio.gather(*(bot.handle_message(NS(message=msg), self.context) for _ in range(3)))
        ask.assert_not_awaited()
        msg.reply_text.assert_not_awaited()
        msg.reply_sticker.assert_awaited_once()

    async def test_emotion_then_text_and_split_texts_prioritize_all_actual_words(self):
        emotion = message(text="😢")
        first = message(text="今天好累")
        second = message(text="刚下班")
        with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                patch.object(bot, "ask_deepseek", AsyncMock(return_value="辛苦啦")) as ask:
            await asyncio.gather(*(bot.handle_message(NS(message=m), self.context) for m in (emotion, first, second)))
        ask.assert_awaited_once_with("10", "今天好累\n刚下班")
        first.reply_text.assert_awaited_once_with("辛苦啦")
        first.reply_sticker.assert_awaited_once()
        emotion.reply_text.assert_not_awaited()

    async def test_pending_emotion_cancelled_by_owner_interruption(self):
        emotion = message(text="❤️", business=True)
        with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                patch.object(bot, "ask_deepseek", AsyncMock()) as ask:
            task = asyncio.create_task(bot.handle_business_message(NS(business_message=emotion), self.context))
            await asyncio.sleep(0)
            owner = message(sender_id=99, message_id=2, text="我回来了", business=True)
            await bot.handle_business_message(NS(business_message=owner), self.context)
            await task
        ask.assert_not_awaited()
        self.api.send_sticker.assert_not_awaited()
        self.assertEqual(bot.pending_replies, {})

    async def test_text_with_no_stickers_request_wins_over_attached_emotion(self):
        text = message(text="今天只回文字")
        emotion = message(text="🥰")
        with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                patch.object(bot, "ask_deepseek", AsyncMock(return_value="好呀[sticker:1]")):
            await asyncio.gather(bot.handle_message(NS(message=text), self.context),
                                 bot.handle_message(NS(message=emotion), self.context))
        text.reply_text.assert_awaited_once_with("好呀")
        text.reply_sticker.assert_not_awaited()

    async def test_pending_batches_do_not_merge_different_contacts(self):
        first, second = message(10, text="你好"), message(20, text="🥰")
        with patch.object(bot.config, "MESSAGE_MERGE_SECONDS", 0.02), \
                patch.object(bot, "ask_deepseek", AsyncMock(return_value="你好呀")) as ask:
            await asyncio.gather(bot.handle_message(NS(message=first), self.context),
                                 bot.handle_message(NS(message=second), self.context))
        ask.assert_awaited_once_with("10", "你好")
        first.reply_sticker.assert_not_awaited()
        second.reply_sticker.assert_awaited_once()

    async def test_async_model_failure_or_empty_answer_preserves_history(self):
        bot.chat_history["10"] = [{"role": "user", "content": "existing"}]
        create = AsyncMock()
        fake_client = NS(chat=NS(completions=NS(create=create)))
        for value in (RuntimeError("offline"), NS(choices=[NS(message=NS(content=None))])):
            create.side_effect = value if isinstance(value, Exception) else None
            create.return_value = value
            with patch.object(bot, "client", fake_client), self.assertLogs(bot.logger, "ERROR"):
                self.assertIsNone(await bot.ask_deepseek("10", "new"))
            self.assertEqual(bot.chat_history["10"], [{"role": "user", "content": "existing"}])

    def test_log_formatter_redacts_secrets_even_in_errors(self):
        formatter = RedactingFormatter(("secret-token",))
        record = logging.LogRecord("test", logging.ERROR, "", 0, "failed URL %s", ("secret-token",), None)
        output = formatter.format(record)
        self.assertNotIn("secret-token", output)
        self.assertIn("[REDACTED]", output)

    def test_real_handler_filters_keep_business_commands_and_ignore_groups(self):
        with patch.object(bot.config, "TG_BOT_TOKEN", "123456:TEST"):
            app = bot.build_application()

        def handlers(update):
            return [h.callback for h in app.handlers[0] if h.check_update(update)]

        business = Update.de_json({"update_id": 1, "business_message": {
            "message_id": 1, "date": 1, "chat": {"id": 10, "type": "private"},
            "from": {"id": 10, "first_name": "Test", "is_bot": False},
            "text": "/reset", "business_connection_id": "connection",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }}, None)
        business.business_message.set_bot(NS(username="testbot"))
        self.assertEqual(handlers(business), [bot.handle_business_message])
        group = Update.de_json({"update_id": 2, "message": {
            "message_id": 2, "date": 1, "chat": {"id": -1, "type": "group", "title": "Test"}, "text": "hi",
        }}, None)
        self.assertEqual(handlers(group), [])


if __name__ == "__main__":
    unittest.main()
