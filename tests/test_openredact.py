import csv
import io
import json
import queue
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib import error

import openredact as app


class FakeResponse:
    def __init__(self, status=204, data=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = b"" if data is None else json.dumps(data).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(status, data=None, headers=None):
    return error.HTTPError("https://discord.com/api/v10/test", status, "test", headers or {}, io.BytesIO(json.dumps(data or {}).encode()))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package.zip"

    def archive(self, files):
        with zipfile.ZipFile(self.package, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return self.package

    def test_user_examples_are_substrings_and_ignore_case(self):
        for content in ["What the fuck are you talking about?", "He's a fuckface", "It smells like shit in here", "I'm shitting right now", "FUCKFACE"]:
            with self.subTest(content=content):
                self.assertTrue(app.matches(content, ("fuck", "shit")))
        self.assertFalse(app.matches("Hello there!", ("fuck", "shit")))

    def test_blacklist_bom_spaces_duplicates_and_empty_entries(self):
        path = self.root / "blacklist.txt"
        path.write_text("\ufeff fuck, SHIT, , fuck,\n", encoding="utf-8")
        self.assertEqual(app.load_blacklist(path), ("fuck", "shit"))
        path.write_text(" , \n, ", encoding="utf-8")
        with self.assertRaises(app.OpenRedactError):
            app.load_blacklist(path)

    def test_literals_are_not_regex_and_internal_spaces_are_preserved(self):
        self.assertTrue(app.matches("contains a.b", ("a.b",)))
        self.assertFalse(app.matches("contains axb", ("a.b",)))
        self.assertFalse(app.matches("two  words", ("two words",)))

    def test_csv_json_uppercase_legacy_paths_and_deduplication(self):
        csv_text = io.StringIO(newline="")
        writer = csv.writer(csv_text)
        writer.writerow(["ID", "Timestamp", "Contents", "Attachments"])
        writer.writerow(["101", "2026-01-01", 'SHIT, "hello"\nsecond line', ""])
        writer.writerow(["102", "2026-01-01", "clean", "https://example.com/shit.png"])
        files = {
            "Messages/c201/messages.csv": "\ufeff" + csv_text.getvalue(),
            "messages/202/messages.json": json.dumps([{"ID": 103, "Contents": "fuckface"}, {"ID": "104", "Contents": None}]),
            "messages/c201/messages.json": json.dumps([{"ID": "101", "Contents": "shit"}]),
            "Account/user.json": json.dumps({"id": "301"}),
            "unrelated/messages.json": "not json",
        }
        scan = app.scan_package(self.archive(files), ("shit", "fuck"))
        self.assertEqual(scan.total, 4)
        self.assertEqual(scan.account_id, "301")
        self.assertEqual([(message.channel_id, message.message_id) for message in scan.messages], [("201", "101"), ("202", "103")])

    def test_csv_preserves_leading_and_trailing_content_whitespace(self):
        path = self.archive({"messages/c1/messages.csv": 'ID,Contents\n2," text "\n'})
        scan = app.scan_package(path, (" text ",))
        self.assertEqual(scan.messages[0].content, " text ")

    def test_malformed_packages_fail_before_deletion(self):
        invalid_files = [
            {"messages/c1/messages.json": "not json"},
            {"messages/c1/messages.json": "{}"},
            {"messages/c1/messages.json": '[{"ID": "2"}]'},
            {"messages/c1/messages.json": '[{"ID": "../../2", "Contents": "shit"}]'},
            {"messages/c1/messages.json": '[{"ID": 2.5, "Contents": "shit"}]'},
            {"messages/c1/messages.json": '[{"ID": true, "Contents": "shit"}]'},
            {"messages/c1/messages.json": '[{"ID": "2", "Contents": 5}]'},
            {"messages/c1/messages.csv": "ID,Text\n2,shit\n"},
            {"messages/c1/messages.csv": 'ID,Contents\n2,"unterminated'},
            {"messages/index.json": "{}"},
        ]
        for files in invalid_files:
            with self.subTest(files=files), self.assertRaises(app.OpenRedactError):
                app.scan_package(self.archive(files), ("shit",))
        self.package.write_bytes(b"not zip")
        with self.assertRaises(app.OpenRedactError):
            app.scan_package(self.package, ("shit",))

    def test_zip_is_never_extracted(self):
        path = self.archive({"../outside.txt": "content", "messages/c1/messages.json": '[{"ID":"2","Contents":"shit"}]'})
        self.assertEqual(len(app.scan_package(path, ("shit",)).messages), 1)
        self.assertFalse((self.root / "outside.txt").exists())


class DiscordTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        client = app.DiscordClient("fake-test-token", opener=opener, sleep=Mock(), output=Mock())
        return client

    def test_delete_endpoint_headers_and_204(self):
        client = self.client([FakeResponse()])
        self.assertEqual(client.delete(app.Message("1", "2", "shit")), "deleted")
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://discord.com/api/v10/channels/1/messages/2")
        self.assertEqual(req.method, "DELETE")
        self.assertEqual(req.get_header("Authorization"), "fake-test-token")

    def test_only_unknown_message_is_already_absent(self):
        for status, code, expected in [(404, 10008, "already absent"), (404, 10003, "failed"), (403, 50001, "failed"), (302, None, "failed")]:
            with self.subTest(status=status, code=code):
                client = self.client([http_error(status, {"code": code})])
                self.assertTrue(client.delete(app.Message("1", "2", "shit")).startswith(expected))

    def test_rate_limit_uses_longer_header_or_body_delay(self):
        client = self.client([http_error(429, {"retry_after": 1.2}, {"Retry-After": "2.5"}), FakeResponse()])
        self.assertEqual(client.delete(app.Message("1", "2", "shit")), "deleted")
        client.sleep.assert_called_once_with(2.75)

    def test_known_deletion_failures_include_the_reason(self):
        cases = [
            (404, 10003, "channel unavailable (Unknown Channel)"),
            (403, 50001, "no access to this channel (Missing Access)"),
            (403, 50013, "missing permission to delete this message"),
            (400, 50021, "this system message cannot be deleted"),
            (403, 50083, "the thread is archived"),
        ]
        for status, code, reason in cases:
            with self.subTest(code=code):
                client = self.client([http_error(status, {"code": code})])
                self.assertEqual(client.delete(app.Message("1", "2", "shit")), f"failed (HTTP {status}, code {code}): {reason}")
                self.assertEqual(client.opener.open.call_count, 1)

    def test_success_reset_header_delays_next_request(self):
        client = self.client([FakeResponse(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "3.5"}), FakeResponse()])
        client.delete(app.Message("1", "2", "shit"))
        client.delete(app.Message("1", "3", "shit"))
        client.sleep.assert_called_once_with(3.75)

    def test_network_and_server_errors_retry(self):
        client = self.client([error.URLError("offline"), http_error(503), FakeResponse()])
        self.assertEqual(client.delete(app.Message("1", "2", "shit")), "deleted")
        self.assertEqual([call.args[0] for call in client.sleep.call_args_list], [1, 2])

    def test_retries_are_bounded(self):
        for failure in [http_error(429), http_error(503), error.URLError("offline")]:
            with self.subTest(failure=failure):
                if isinstance(failure, error.HTTPError):
                    failures = [http_error(failure.code) for _ in range(6)]
                else:
                    failures = [failure] * 6
                client = self.client(failures)
                with self.assertRaises(app.OpenRedactError):
                    client.delete(app.Message("1", "2", "shit"))
                self.assertEqual(client.opener.open.call_count, 6)

    def test_invalid_waits_do_not_make_sleep_fail(self):
        for wait in ["nan", "inf", -1, "bad", None]:
            with self.subTest(wait=wait):
                client = self.client([http_error(429, {"retry_after": wait}), FakeResponse()])
                client.delete(app.Message("1", "2", "shit"))
                client.sleep.assert_called_once_with(1.25)

    def test_authentication_failure_stops_without_retry(self):
        client = self.client([http_error(401)])
        with self.assertRaises(app.OpenRedactError):
            client.current_user()
        self.assertEqual(client.opener.open.call_count, 1)

    def test_account_identity_requires_successful_valid_response(self):
        client = self.client([FakeResponse(200, {"id": "301"})])
        self.assertEqual(client.current_user(), "301")
        for response in [FakeResponse(403), FakeResponse(200, {}), FakeResponse(200, {"id": False})]:
            with self.subTest(response=response), self.assertRaises(app.OpenRedactError):
                self.client([response]).current_user()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com"))

    def test_summary_distinguishes_outcomes_and_paces(self):
        client = self.client([FakeResponse(), http_error(404, {"code": 10008}), http_error(403)])
        with patch("builtins.print") as output:
            status = app.delete_messages(client, [app.Message("1", str(i), "shit") for i in range(2, 5)])
        self.assertEqual(status, 1)
        self.assertEqual(client.sleep.call_count, 2)
        output.assert_any_call("  Results: 1 deleted, 1 absent, 1 failed, 0 remaining.")
        self.assertEqual(output.call_count, 1)

    def test_interruption_shows_completed_and_unprocessed_counts(self):
        client = self.client([FakeResponse(), KeyboardInterrupt()])
        with patch("builtins.print") as output, self.assertRaises(KeyboardInterrupt):
            app.delete_messages(client, [app.Message("1", "2", "shit"), app.Message("1", "3", "shit")])
        output.assert_any_call("  Results: 1 deleted, 0 absent, 0 failed, 1 remaining.")

    def test_verbose_output_includes_retry_notices_and_attempts(self):
        client = self.client([http_error(429, {"retry_after": 1}), FakeResponse()])
        original_output = client.output
        stream = io.StringIO()
        console = app.Console(no_color=True, stream=stream)
        self.assertEqual(app.delete_messages(client, [app.Message("1", "2", "shit")], console=console, verbose=True), 0)
        self.assertIn("Rate limited; waiting", stream.getvalue())
        self.assertIn("[1/1] 1/2: deleted", stream.getvalue())
        self.assertIn("Results: 1 deleted, 0 absent, 0 failed, 0 remaining.", stream.getvalue())
        self.assertIs(client.output, original_output)

    def test_quiet_output_has_no_message_details_or_terminal_controls(self):
        client = self.client([FakeResponse(), http_error(403, {"code": 50001})])
        stream = io.StringIO()
        console = app.Console(no_color=True, stream=stream)
        app.delete_messages(client, [app.Message("111", "222", "shit"), app.Message("111", "333", "shit")], console=console)
        self.assertEqual(stream.getvalue(), "  Results: 1 deleted, 0 absent, 1 failed, 0 remaining.\n")

    def test_fatal_error_keeps_unfinished_messages_remaining_and_restores_client(self):
        client = self.client([FakeResponse(), http_error(401)])
        original_output = client.output
        stream = io.StringIO()
        console = app.Console(no_color=True, stream=stream)
        with self.assertRaises(app.OpenRedactError):
            app.delete_messages(client, [app.Message("1", str(i), "shit") for i in range(2, 5)], console=console)
        self.assertIn("1 deleted, 0 absent, 0 failed, 2 remaining", stream.getvalue())
        self.assertIs(client.output, original_output)

    def test_v_shows_previous_details_while_request_is_in_flight(self):
        in_flight = threading.Event()
        details_seen = threading.Event()
        keys = queue.Queue()
        keys.put("v")

        class TerminalStream(io.StringIO):
            def write(self, text):
                if "[1/2] 111/222: deleted" in text:
                    details_seen.set()
                return super().write(text)

        class FakeKeyboard:
            def __init__(self, *args):
                self.active = True
                self.review_keys = iter(["v", "\r"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                if threading.current_thread() is threading.main_thread():
                    return next(self.review_keys)
                if in_flight.is_set():
                    try:
                        return keys.get_nowait()
                    except queue.Empty:
                        pass
                return ""

        def second_request():
            in_flight.set()
            if not details_seen.wait(3):
                raise AssertionError("V did not reveal the earlier attempt while the request was blocked")
            return "already absent"

        client = Mock()
        client.pending_delay = 0.0
        client.delete.side_effect = lambda message: "deleted" if message.message_id == "222" else second_request()
        stream = TerminalStream()
        console = app.Console(no_color=True, stream=stream)
        console.terminal = True
        console.ansi = True
        with patch.object(app, "Keyboard", FakeKeyboard):
            self.assertEqual(app.delete_messages(client, [app.Message("111", "222", "shit"), app.Message("111", "333", "shit")], console=console), 0)
        self.assertTrue(details_seen.is_set())
        self.assertIn("[2/2] 111/333: already absent", stream.getvalue())
        self.assertIn("Results: 1 deleted, 1 absent, 0 failed, 0 remaining.", stream.getvalue())
        self.assertTrue(console.reviewed)

    def test_repeated_v_keeps_all_421_attempts_and_nine_failures_visible(self):
        first_batch_visible = threading.Event()
        repeated_keys_read = threading.Event()
        keys = queue.Queue()
        keys.put("v")

        class TerminalStream(io.StringIO):
            def write(self, text):
                if "[15/421]" in text:
                    first_batch_visible.set()
                return super().write(text)

        class FakeKeyboard:
            def __init__(self, *args):
                self.active = True
                self.read_count = 0
                self.review_keys = iter(["v", "home"] + ["page_down"] * 30 + ["\r"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                if threading.current_thread() is threading.main_thread():
                    return next(self.review_keys)
                try:
                    key = keys.get_nowait()
                except queue.Empty:
                    return ""
                self.read_count += 1
                if self.read_count == 4:
                    repeated_keys_read.set()
                return key

        failures = {11, 14, 15, 16, 17, 18, 19, 20, 21}

        def delete(message):
            index = int(message.message_id)
            if index == 16:
                if not first_batch_visible.wait(3):
                    raise AssertionError("The first batch was not displayed")
                for _ in range(3):
                    keys.put("v")
                if not repeated_keys_read.wait(3):
                    raise AssertionError("Repeated keypresses were not handled")
            return "failed (HTTP 403, code 50001): no access to this channel" if index in failures else "deleted"

        client = Mock()
        client.pending_delay = 0.0
        client.delete.side_effect = delete
        stream = TerminalStream()
        console = app.Console(no_color=True, stream=stream)
        console.terminal = True
        console.ansi = True
        with patch.object(app, "Keyboard", FakeKeyboard):
            self.assertEqual(app.delete_messages(client, [app.Message("111", str(i), "shit") for i in range(1, 422)], console=console), 1)
        output = stream.getvalue()
        self.assertGreaterEqual(output.count("failed (HTTP 403, code 50001)"), 9)
        for index in range(1, 422):
            self.assertIn(f"[{index}/421]", output)
        self.assertIn("Results: 412 deleted, 0 absent, 9 failed, 0 remaining.", output)

    def test_summary_collapses_log_and_reopening_keeps_history(self):
        stream = io.StringIO()
        console = app.Console(no_color=True, stream=stream)
        console.terminal = console.ansi = True
        progress = app.DeletionProgress(3, console)
        progress.live = True
        progress.record(1, app.Message("111", "222", "shit"), "deleted")
        progress.record(2, app.Message("111", "333", "shit"), "failed (HTTP 403, code 50001)")
        progress.draw()
        progress.handle_key("v")
        progress.draw()
        first_view = stream.getvalue().split("\033[?1049h", 1)
        self.assertNotIn("111/222", first_view[0])
        self.assertIn("111/222", first_view[1])
        self.assertIn("111/333", first_view[1])
        progress.handle_key("s")
        progress.draw()
        restored_summary = stream.getvalue().split("\033[?1049l", 1)[1]
        self.assertIn("[summary]", restored_summary)
        self.assertIn("1 deleted, 0 absent, 1 failed, 1 remaining", restored_summary)
        self.assertNotIn("111/222", restored_summary)
        self.assertNotIn("111/333", restored_summary)
        progress.record(3, app.Message("111", "444", "shit"), "already absent")
        progress.handle_key("v")
        progress.draw()
        reopened = stream.getvalue().rsplit("\033[?1049h", 1)[1]
        for identifier in ("111/222", "111/333", "111/444"):
            self.assertIn(identifier, reopened)
        progress.close_details()

    def test_details_view_wraps_long_errors_without_overflowing_on_resize(self):
        stream = io.StringIO()
        console = app.Console(no_color=True, stream=stream)
        progress = app.DeletionProgress(60, console, verbose=True)
        progress.live = True
        for index in range(1, 61):
            progress.record(index, app.Message("111111111111111111", str(index), "shit"), "failed (HTTP 403, code 50001): no access to this channel (Missing Access)")
        for columns, lines in [(80, 24), (48, 12), (110, 30)]:
            with self.subTest(columns=columns), patch.object(app.shutil, "get_terminal_size", return_value=app.os.terminal_size((columns, lines))):
                progress.handle_key("home")
                stream.seek(0)
                stream.truncate()
                progress.draw()
                frame = stream.getvalue().split("\033[H", 1)[1].split("\033[J", 1)[0]
                rendered_lines = frame.replace("\033[2K", "").split("\r\n")
                self.assertLessEqual(len(rendered_lines), lines)
                self.assertTrue(all(len(line) <= columns - 1 for line in rendered_lines))
                self.assertIn("[1/60]", frame)
                progress.handle_key("end")
                stream.seek(0)
                stream.truncate()
                progress.draw()
                self.assertIn("[60/60]", stream.getvalue())
        progress.close_details()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package with spaces.zip"
        self.blacklist = self.root / "blacklist.txt"
        self.config = self.root / "config.json"
        self.blacklist.write_text("fuck, shit", encoding="utf-8")
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("messages/c1/messages.json", json.dumps([{"ID": "2", "Contents": "fuckface"}, {"ID": "3", "Contents": "shitting"}, {"ID": "4", "Contents": "hello"}]))
            archive.writestr("account/user.json", '{"id":"301"}')

    def inputs(self, confirmation):
        return [f'"{self.package}"', str(self.blacklist), "fake-test-token", confirmation]

    def test_cancel_saves_config_without_network(self):
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=self.inputs("no")), patch.object(app, "DiscordClient") as client, patch("builtins.print"):
            self.assertEqual(app.run([]), 0)
        client.assert_not_called()
        self.assertEqual(app.load_config(self.config), {"token": "fake-test-token", "package_path": str(self.package.resolve()), "blacklist_path": str(self.blacklist.resolve())})
        self.assertEqual(list(self.root.glob("tmp*")), [])

    def test_dry_run_never_prompts_for_token_or_writes_config(self):
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=self.inputs("unused")), patch.object(app, "prompt_token") as token, patch.object(app, "DiscordClient") as client, patch("builtins.print"):
            self.assertEqual(app.run(["--dry-run"]), 0)
        token.assert_not_called()
        client.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_confirmed_flow_deletes_only_matching_messages(self):
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=self.inputs("DELETE")), patch.object(app, "DiscordClient") as constructor, patch("builtins.print"):
            client = constructor.return_value
            client.pending_delay = 0.0
            client.current_user.return_value = "301"
            client.delete.return_value = "deleted"
            self.assertEqual(app.run([]), 0)
        self.assertEqual([call.args[0].message_id for call in client.delete.call_args_list], ["2", "3"])

    def test_mismatched_account_prevents_all_deletions(self):
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=self.inputs("DELETE")), patch.object(app, "DiscordClient") as constructor, patch("builtins.print"):
            constructor.return_value.current_user.return_value = "999"
            with self.assertRaises(app.OpenRedactError):
                app.run([])
        constructor.return_value.delete.assert_not_called()

    def test_saved_token_can_be_reused_or_replaced(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(app.prompt_token("saved-token"), "saved-token")
        with patch("builtins.input", return_value="new-token"):
            self.assertEqual(app.prompt_token("saved-token"), "new-token")

    def test_all_saved_values_can_be_reused_without_retyping(self):
        config = {"package_path": str(self.package.resolve()), "blacklist_path": str(self.blacklist.resolve()), "token": "saved-token", "custom_setting": True}
        app.save_config(self.config, config)
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=["", "", "", "no"]), patch.object(app, "DiscordClient") as client, patch("builtins.print"):
            self.assertEqual(app.run([]), 0)
        client.assert_not_called()
        self.assertEqual(app.load_config(self.config), config)

    def test_legacy_token_config_keeps_token_when_saving_paths(self):
        app.save_config(self.config, {"token": "saved-token"})
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=[str(self.package), str(self.blacklist), "", "no"]), patch("builtins.print"):
            self.assertEqual(app.run([]), 0)
        self.assertEqual(app.load_config(self.config), {"token": "saved-token", "package_path": str(self.package.resolve()), "blacklist_path": str(self.blacklist.resolve())})

    def test_paths_are_saved_even_if_user_stops_before_token_prompt(self):
        app.save_config(self.config, {"token": "saved-token"})
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=[str(self.package), str(self.blacklist), EOFError()]), patch("builtins.print"), self.assertRaises(EOFError):
            app.run([])
        config = app.load_config(self.config)
        self.assertEqual(config["token"], "saved-token")
        self.assertEqual(config["package_path"], str(self.package.resolve()))
        self.assertEqual(config["blacklist_path"], str(self.blacklist.resolve()))

    def test_saved_paths_can_be_replaced(self):
        app.save_config(self.config, {"package_path": str(self.blacklist), "blacklist_path": str(self.package), "token": "saved-token"})
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=self.inputs("no")), patch("builtins.print"):
            self.assertEqual(app.run([]), 0)
        config = app.load_config(self.config)
        self.assertEqual(config["package_path"], str(self.package.resolve()))
        self.assertEqual(config["blacklist_path"], str(self.blacklist.resolve()))

    def test_missing_saved_path_requires_a_replacement(self):
        with patch("builtins.input", side_effect=["", str(self.package)]), patch("builtins.print") as output:
            path = app.prompt_path("Package", saved=str(self.root / "missing.zip"))
        self.assertEqual(path, self.package.resolve())
        output.assert_any_call("  The saved file no longer exists. Enter a new path.")

    def test_ctrl_v_fallback_does_not_save_control_characters(self):
        with patch("builtins.input", side_effect=["\x16", "^V", "new-token"]), patch("builtins.print") as output:
            self.assertEqual(app.prompt_token("saved-token"), "new-token")
        self.assertEqual(output.call_count, 2)
        output.assert_any_call("  Ctrl+V is not supported by this console. Right-click to paste, then press Enter.")

    def test_dry_run_reuses_paths_without_changing_existing_config(self):
        config = {"package_path": str(self.package.resolve()), "blacklist_path": str(self.blacklist.resolve()), "token": "saved-token"}
        app.save_config(self.config, config)
        before = self.config.read_bytes()
        with patch.object(app, "CONFIG_PATH", self.config), patch("builtins.input", side_effect=["", ""]), patch.object(app, "prompt_token") as token, patch("builtins.print"):
            self.assertEqual(app.run(["--dry-run"]), 0)
        token.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_malformed_config_is_not_overwritten(self):
        for content in ["not json", "[]", '{"token":123}', '{"package_path":123}', '{"blacklist_path":null}']:
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                with self.assertRaises(app.OpenRedactError):
                    app.load_config(self.config)
                self.assertEqual(self.config.read_text(), content)


if __name__ == "__main__":
    unittest.main()
