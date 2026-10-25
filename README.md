# OpenRedact

Discord message purging, made simple.

## What is this?

OpenRedact is a command-line tool that reads your Discord `package.zip` and deletes your messages if they contain anything from your blacklist.

You choose the words. OpenRedact finds the matches, tells you how many it found, and asks before deleting anything.

Use OpenRedact at your own risk. I am not responsible for anything that happens to your Discord account as a result of using it, including restrictions, suspension, or a ban.

## How do I run it?

You'll need Python 3.10 or newer, your Discord data package, and your Discord account token. There are no extra packages to install.

1. Download this repository and extract it.
2. Create a `blacklist.txt` file with the words you want to look for, separated by commas.
3. On Windows, double-click `OpenRedact.cmd`.
4. Follow the prompts to enter your package path, blacklist path, and token. Right-click to paste.
5. Check the number of matches, then type `DELETE` to start. Anything else cancels.

You can also open a terminal in the project folder and run:

```sh
python openredact.py
```

OpenRedact saves your paths and token in `config.json`. Next time, press Enter at each prompt to reuse them, or type something new to replace them.

Your token is visible when you enter it and is saved as plain text. Keep `config.json` private. Delete it if you want to clear your saved settings.

## How does the blacklist work?

Put your words in `blacklist.txt` like this:

```text
fuck, shit
```

Matches ignore capitalization and include parts of words. For example, `fuck` also matches `fuckface`, and `shit` also matches `shitting`. You can use phrases too.

There's an example file included: `blacklist.example.txt`.

## Can I check the matches without deleting anything?

Yes. Run:

```sh
python openredact.py --dry-run
```

This shows how many messages match. It doesn't ask for your token, contact Discord, or change your saved settings.

## What happens while it's deleting?

You'll see a spinner and a running count:

```text
/ [summary] 5 deleted, 0 absent, 0 failed, 4 remaining
```

- **Deleted:** Deleted during this run.
- **Absent:** The message was already gone.
- **Failed:** Discord rejected the deletion.
- **Remaining:** Messages the app hasn't finished handling yet.

Press **V** to open the detailed log and **S** to collapse it. Use the arrow keys or Page Up/Page Down to scroll. Home jumps to the beginning; End follows the latest entries.

Press **Ctrl+C** to stop deletion. You can rerun the app later to try again. When the run finishes, you can still open the log with **V**, or press **Enter** to close.

## Why did some messages fail?

Press **V** to see the reason. A common cause is losing access to a server or channel. OpenRedact can't delete messages in channels your account can't access.

It also works from the text in your data export. Messages sent after that export aren't included, and later edits won't change which messages it matches.

## Any other options?

- `--verbose` opens the detailed log from the start. If you're saving terminal output to a file, it prints every attempt instead.
- `--no-color` turns off colors.

You can combine options, for example:

```sh
python openredact.py --dry-run --no-color
```

## Credits

OpenRedact is based on the package-reading and message-deletion approach in [Data Package Tool 1.4.0](https://github.com/aamiaa/Data-Package-Tool). Thanks to its author for the starting point.

The code, tests, and documentation were created with assistance from Codex, OpenAI's AI coding assistant.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

The tests use fake exports and simulated Discord responses. They don't delete real messages.
