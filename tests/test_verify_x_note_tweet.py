from __future__ import annotations

import unittest

from scripts import verify_x_note_tweet


class VerifyXNoteTweetTests(unittest.TestCase):
    def response(
        self,
        *,
        text: str,
        urls: list[dict[str, object]],
        parent_status_id: str = "111",
        status_id: str = "222",
    ) -> dict[str, object]:
        return {
            "data": {
                "id": status_id,
                "created_at": "2026-07-31T05:00:00Z",
                "conversation_id": "111",
                "referenced_tweets": [
                    {"type": "replied_to", "id": parent_status_id}
                ],
                "note_tweet": {
                    "text": text,
                    "entities": {"urls": urls},
                },
            }
        }

    def test_reconstructs_codepoint_url_entity(self) -> None:
        short_url = "https://t.co/abc"
        expanded_url = "https://example.com/source"
        prefix = "Факт: "
        suffix = " подтвержден."
        response = self.response(
            text=prefix + short_url + suffix,
            urls=[
                {
                    "start": len(prefix),
                    "end": len(prefix) + len(short_url),
                    "url": short_url,
                    "expanded_url": expanded_url,
                }
            ],
        )
        source = prefix + expanded_url + suffix

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(report["valid"])
        self.assertTrue(report["exact_file_match"])
        self.assertEqual(report["entity_index_modes"], ["codepoint"])

    def test_reconstructs_utf16_url_entity_after_emoji(self) -> None:
        short_url = "https://t.co/x"
        expanded_url = "https://example.com/x"
        prefix = "🙂 "
        response = self.response(
            text=prefix + short_url,
            urls=[
                {
                    "start": 3,
                    "end": 3 + len(short_url),
                    "url": short_url,
                    "expanded_url": expanded_url,
                }
            ],
        )
        source = prefix + expanded_url

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["entity_index_modes"], ["utf16"])

    def test_rejects_source_text_mismatch(self) -> None:
        response = self.response(text="Точный опубликованный текст", urls=[])

        report = verify_x_note_tweet.build_report(
            response,
            source_text="Другой локальный текст",
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertFalse(report["valid"])
        self.assertFalse(report["exact_file_match"])

    def test_rejects_wrong_parent_and_forbidden_character(self) -> None:
        source = "Текст с запрещенным символом \u2014 здесь"
        response = self.response(
            text=source,
            urls=[],
            parent_status_id="999",
        )

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertFalse(report["valid"])
        self.assertFalse(report["parent_matches"])
        self.assertEqual(report["forbidden"], ["\u2014"])

    def test_accepts_short_reply_and_rejects_empty_or_oversized(self) -> None:
        short = "Короткий проверенный ответ"
        accepted = verify_x_note_tweet.build_report(
            self.response(text=short, urls=[]),
            source_text=short,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )
        empty = verify_x_note_tweet.build_report(
            self.response(text="", urls=[]),
            source_text="",
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )
        oversized_text = "р" * 4001
        oversized = verify_x_note_tweet.build_report(
            self.response(text=oversized_text, urls=[]),
            source_text=oversized_text,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(accepted["valid"])
        self.assertTrue(accepted["non_empty"])
        self.assertTrue(accepted["length_within_limit"])
        self.assertFalse(empty["valid"])
        self.assertFalse(empty["non_empty"])
        self.assertFalse(oversized["valid"])
        self.assertFalse(oversized["length_within_limit"])

    def test_accepts_regular_short_tweet_without_note_tweet(self) -> None:
        source = "Короткий обычный ответ"
        response = {
            "data": {
                "id": "222",
                "text": source,
                "created_at": "2026-08-02T15:00:00Z",
                "conversation_id": "111",
                "referenced_tweets": [
                    {"type": "replied_to", "id": "111"}
                ],
                "entities": {"urls": []},
            }
        }

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["text_source"], "text")

    def test_removes_only_entity_backed_hidden_reply_mentions(self) -> None:
        source = "Короткий обычный ответ"
        prefix = "@DonHuanMatuss @grytsig "
        first = "@DonHuanMatuss"
        second = "@grytsig"
        response = {
            "data": {
                "id": "222",
                "text": prefix + source,
                "conversation_id": "111",
                "referenced_tweets": [
                    {"type": "replied_to", "id": "111"}
                ],
                "entities": {
                    "mentions": [
                        {
                            "start": 0,
                            "end": len(first),
                            "username": "DonHuanMatuss",
                        },
                        {
                            "start": len(first) + 1,
                            "end": len(first) + 1 + len(second),
                            "username": "grytsig",
                        },
                    ]
                },
            }
        }

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["api_code_points"], len(prefix + source))
        self.assertEqual(
            report["reply_prefix_mentions"], ["DonHuanMatuss", "grytsig"]
        )
        self.assertEqual(report["text_normalization"], "hidden_reply_mentions")

    def test_does_not_strip_unverified_text_prefix(self) -> None:
        source = "Короткий обычный ответ"
        response = {
            "data": {
                "id": "222",
                "text": "Чужой префикс " + source,
                "conversation_id": "111",
                "referenced_tweets": [
                    {"type": "replied_to", "id": "111"}
                ],
                "entities": {"mentions": []},
            }
        }

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertFalse(report["valid"])
        self.assertFalse(report["exact_file_match"])
        self.assertEqual(report["text_normalization"], "none")

    def test_prefers_note_tweet_when_both_text_forms_exist(self) -> None:
        response = self.response(text="Полный длинный ответ", urls=[])
        response["data"]["text"] = "Сокращенная версия"

        report = verify_x_note_tweet.build_report(
            response,
            source_text="Полный длинный ответ",
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["text_source"], "note_tweet")

    def test_requires_and_records_published_photo(self) -> None:
        source = "Готово, вот запрошенная инфографика."
        response = self.response(text=source, urls=[])
        response["data"]["attachments"] = {"media_keys": ["3_photo"]}
        response["includes"] = {
            "media": [
                {
                    "media_key": "3_photo",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/example.jpg",
                    "width": 1024,
                    "height": 1024,
                }
            ]
        }

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
            require_media=True,
        )

        self.assertTrue(report["valid"])
        self.assertTrue(report["media_verified"])
        self.assertEqual(report["media_count"], 1)
        self.assertEqual(report["published_media_types"], ["photo"])

    def test_rejects_required_media_when_expansion_is_missing(self) -> None:
        source = "Готово."
        response = self.response(text=source, urls=[])
        response["data"]["attachments"] = {"media_keys": ["3_missing"]}

        report = verify_x_note_tweet.build_report(
            response,
            source_text=source,
            maximum_length=4000,
            expected_status_id="222",
            expected_parent_status_id="111",
            require_media=True,
        )

        self.assertFalse(report["valid"])
        self.assertFalse(report["media_verified"])
        self.assertFalse(report["media_expansion_complete"])


if __name__ == "__main__":
    unittest.main()
