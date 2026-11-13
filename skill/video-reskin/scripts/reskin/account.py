"""Account and runtime commands: auth (Winky key, Lumos login), login (Feishu QR) and runtime."""
from __future__ import annotations

import argparse
import json
import sys

from reskin import credentials, net, runtime

RULE_FAILURE = 2


def show(lines) -> None:
    print("\n".join(lines) if isinstance(lines, list) else lines, flush=True)


def cmd_auth(args) -> int:
    from reskin import lumos, winky
    if args.key:
        credentials.save_winky_key(args.key)
    if args.forget_login:
        credentials.forget_lumos_login()
    summary = credentials.summary()
    lines = [f"credentials: {summary['file']}",
             f"winky key: {summary['winky_key'] or 'missing (auth --key <key>)'}"
             + (f" ({summary['winky_key_source']})" if summary["winky_key"] else "")]
    if summary["winky_key"]:
        checked = winky.check_key()
        lines.append(f"winky: key {checked['key']} (Gemini review)")
    try:
        live = lumos.user()
        lines.append(f"lumos: logged in as {live.get('name')} ({live.get('user_id')})")
    except credentials.MissingCredential as error:
        lines.append(f"lumos: {error}")
    show(lines)
    return 0


def cmd_login(args) -> int:
    if args.check:
        from reskin import lumos
        live = lumos.user()
        show(f"lumos: logged in as {live.get('name')} ({live.get('user_id')})")
        return 0
    runtime.ensure("media")
    runtime.ensure("login")
    from reskin import login
    result = login.run(timeout=args.timeout, open_image=not args.no_open)
    show([f"lumos: logged in as {result['name']} ({result['user_id']})", "next: init (or continue the job)"])
    return 0


def cmd_runtime(args) -> int:
    if args.install:
        runtime.ensure(args.feature)
        if args.feature == "media":
            runtime.binary("ffmpeg"); runtime.binary("ffprobe")
    from verify_runtime import verify
    report = verify(args.feature)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["decision"] == "pass" else 3


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, help=None):
        action = sub.add_parser(name, help=help)
        action.set_defaults(handler=handler)
        return action

    auth = command("auth", cmd_auth, help="store or check the Winky API key and the Lumos login")
    auth.add_argument("--key", help="the user's Winky API key (stored encrypted for this Windows user)")
    auth.add_argument("--forget-login", action="store_true", help="drop the stored Lumos login")
    login = command("login", cmd_login, help="Feishu QR login to Lumos (opens the QR image on screen)")
    login.add_argument("--timeout", type=float, default=300, help="seconds to wait for the scan (QR codes refresh on their own)")
    login.add_argument("--no-open", action="store_true", help="only write the QR image, do not open it")
    login.add_argument("--check", action="store_true", help="only check the stored login")
    inspection = command("runtime", cmd_runtime, help="inspect or prefetch the private runtime")
    inspection.add_argument("--feature", choices=("base", "media", "login"), default="base")
    inspection.add_argument("--install", action="store_true")
    args = parser.parse_args()
    try:
        code = args.handler(args)
    except credentials.MissingCredential as error:
        print(f"credential error: {error}", file=sys.stderr, flush=True)
        code = RULE_FAILURE
    except runtime.RuntimeFailure as error:
        print(f"runtime error: {error}", file=sys.stderr, flush=True)
        code = 3
    except (ValueError, FileExistsError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        code = RULE_FAILURE
    except (OSError, ImportError) as error:
        print(f"runtime error: {error}; retry the command after fixing the runtime", file=sys.stderr, flush=True)
        code = 3
    except (RuntimeError, net.HttpError) as error:
        print(f"service error: {error}", file=sys.stderr, flush=True)
        code = 3
    raise SystemExit(code)


if __name__ == "__main__":
    main()
