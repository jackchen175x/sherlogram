#!/usr/bin/env python3
"""
Telegram Session String Generator

This script generates a session string that can be used for Telegram authentication
with the Telegram MCP server. The session string allows for portable authentication
without storing session files.

Usage:
    python session_string_generator.py

Requirements:
    - telethon
    - python-dotenv

Note on ID Formats:
When using the MCP server, please be aware that all `chat_id` and `user_id`
parameters support integer IDs, string representations of IDs (e.g., "123456"),
and usernames (e.g., "@mychannel").
"""

import argparse
import asyncio
import getpass
import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import errors
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

load_dotenv()


def _qr_login(
    client: TelegramClient,
    qr_output: str | None = None,
    timeout: int = 120,
) -> None:
    import qrcode

    qr = client.qr_login()

    print("\n----- QR Code Login -----\n")

    qr_obj = qrcode.QRCode(border=1)
    qr_obj.add_data(qr.url)
    qr_obj.make(fit=True)
    f = io.StringIO()
    qr_obj.print_ascii(out=f, invert=True)
    print(f.getvalue())

    if qr_output:
        qr_path = Path(qr_output).expanduser().resolve()
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_obj.make_image(fill_color="black", back_color="white").save(qr_path)
        print(f"QR image saved to: {qr_path}")

    print("Scan the QR code above with your Telegram app:")
    print("  Open Telegram > Settings > Devices > Link Desktop Device\n")
    print(f"Or open this link on a device where you're logged in:\n  {qr.url}\n")
    print(f"Expires at: {qr.expires.strftime('%H:%M:%S')}")
    print("Waiting for you to scan...")

    try:
        client.loop.run_until_complete(qr.wait(timeout=timeout))
    except asyncio.TimeoutError:
        print("\nQR code expired. Please try again.")
        client.disconnect()
        sys.exit(1)
    except errors.SessionPasswordNeededError:
        pw = getpass.getpass("\nTwo-factor authentication enabled. Please enter your password (输入不显示): ")
        client.sign_in(password=pw)


def _phone_login(client: TelegramClient) -> None:
    phone = input("Please enter your phone (or bot token): ")

    try:
        client.send_code_request(phone)
    except errors.FloodWaitError as e:
        print(f"\nFlood wait error; you must wait {e.seconds} seconds before trying again.")
        client.disconnect()
        sys.exit(1)
    except errors.PhoneNumberInvalidError:
        print("\nThe phone number is invalid.")
        client.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"\nError sending code: {e}")
        client.disconnect()
        sys.exit(1)

    code = input("\nPlease enter the code you received: ")
    try:
        client.sign_in(phone, code)
    except errors.SessionPasswordNeededError:
        pw = getpass.getpass("Two-factor authentication enabled. Please enter your password (输入不显示): ")
        client.sign_in(password=pw)


def _update_env_session(session_string: str, env_path: str = ".env") -> None:
    path = Path(env_path).expanduser().resolve()
    env_contents = path.read_text(encoding="utf-8").splitlines(keepends=True)

    session_string_line_found = False
    for i, line in enumerate(env_contents):
        if line.startswith("TELEGRAM_SESSION_STRING="):
            env_contents[i] = f"TELEGRAM_SESSION_STRING={session_string}\n"
            session_string_line_found = True
            break

    if not session_string_line_found:
        env_contents.append(f"TELEGRAM_SESSION_STRING={session_string}\n")

    path.write_text("".join(env_contents), encoding="utf-8")
    # session string 等同于账号完全访问权，不该是默认的 0644（同机其他用户可读）
    path.chmod(0o600)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Telegram session string.")
    parser.add_argument(
        "--method",
        choices=("qr", "phone"),
        help="Choose a login method without an interactive prompt.",
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help="Update TELEGRAM_SESSION_STRING in the .env file automatically.",
    )
    parser.add_argument(
        "--no-print-session",
        action="store_true",
        help="Do not print the generated session secret to stdout.",
    )
    parser.add_argument(
        "--qr-output",
        help="Optional path for a PNG copy of the QR code.",
    )
    parser.add_argument(
        "--qr-timeout",
        type=int,
        default=120,
        help="Seconds to wait for QR login (default: 120).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Environment file to update (default: .env).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    API_ID = os.getenv("TELEGRAM_API_ID")
    API_HASH = os.getenv("TELEGRAM_API_HASH")

    if not API_ID or not API_HASH:
        print("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env file")
        print("Create an .env file with your credentials from https://my.telegram.org/apps")
        sys.exit(1)

    try:
        API_ID = int(API_ID)
    except ValueError:
        print("Error: TELEGRAM_API_ID must be an integer")
        sys.exit(1)

    print("\n----- Telegram Session String Generator -----\n")
    print("This script will generate a session string for your Telegram account.")
    print("The generated session string can be added to your .env file.")
    print(
        "\nYour credentials will NOT be stored on any server and are only used for local authentication.\n"
    )

    if args.method:
        method = "1" if args.method == "qr" else "2"
    else:
        print("Choose login method:")
        print("  1) QR code login (recommended -- scan from your Telegram app)")
        print("  2) Phone number + verification code")
        method = input("\nEnter 1 or 2 [default: 1]: ").strip() or "1"

    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        client.connect()

        if not client.is_user_authorized():
            if method == "1":
                _qr_login(client, qr_output=args.qr_output, timeout=args.qr_timeout)
            else:
                _phone_login(client)

        session_string = StringSession.save(client.session)

        print("\nAuthentication successful!")
        if not args.no_print_session:
            print("\n----- Your Session String -----")
            print(f"\n{session_string}\n")
            print("Add this to your .env file as:")
            print(f"TELEGRAM_SESSION_STRING={session_string}")
            print("\nIMPORTANT: Keep this string private and never share it with anyone!")

        update_env = args.update_env
        if not args.update_env:
            choice = input(
                "\nWould you like to automatically update your .env file with this session string? (y/N): "
            )
            update_env = choice.lower() == "y"

        if update_env:
            try:
                _update_env_session(session_string, args.env_file)
                print(f"\n{args.env_file} updated successfully!")
            except Exception as e:
                print(f"\nError updating {args.env_file}: {e}")
                print("Please manually add the session string to your .env file.")

        client.disconnect()

    except Exception as e:
        print(f"\nError: {e}")
        print("Failed to generate session string. Please try again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
