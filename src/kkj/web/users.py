"""
Stockwise accounts from the command line (the same as Settings -> Users in the app).

  python -m kkj.web.users list
  python -m kkj.web.users add <username> --name "Display Name"   # asks for the password (hidden)
  python -m kkj.web.users reset <username>                        # new password; signs them out everywhere
  python -m kkj.web.users disable <username>                      # can't sign in; signed out now
  python -m kkj.web.users enable <username>                       # also clears a lock-out
  python -m kkj.web.users signout <username>                      # end all their sessions
  python -m kkj.web.users log [--limit 30]                        # recent sign-in attempts

Runs as erp_etl (owner of schema auth). Passwords are typed at a hidden prompt, never
passed as arguments (they would stay in the shell history) and never printed.
"""
import argparse
import getpass
import sys

from sqlalchemy import text

from kkj import settings
from kkj.web.auth import hash_password, password_problem


def _ask_password(username):
    for _ in range(3):
        first = getpass.getpass(f"New password for {username}: ")
        problem = password_problem(first, username)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Again: ") != first:
            print("They don't match.")
            continue
        return first
    sys.exit("No password set.")


def _user(conn, username):
    row = conn.execute(text("SELECT user_id, username FROM auth.app_user WHERE lower(username) = lower(:u)"),
                       {"u": username}).first()
    if row is None:
        sys.exit(f"No account called {username}.")
    return row


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m kkj.web.users", description="Manage Stockwise accounts")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--name", required=True, help="shown in the app and recorded with every approval")
    for cmd in ("reset", "disable", "enable", "signout"):
        sub.add_parser(cmd).add_argument("username")
    sub.add_parser("log").add_argument("--limit", type=int, default=30)
    args = p.parse_args(argv)

    with settings.etl_engine().begin() as conn:
        if args.cmd == "list":
            rows = conn.execute(text("""
                SELECT u.username, u.display_name, u.active, u.last_login_at, u.locked_until > now() AS locked,
                       (SELECT count(*) FROM auth.session s WHERE s.user_id = u.user_id AND s.expires_at > now()) AS sessions
                FROM auth.app_user u ORDER BY u.username""")).all()
            if not rows:
                print("No accounts yet. Add one: python -m kkj.web.users add <username> --name \"Name\"")
            for r in rows:
                state = "active" if r.active else "disabled"
                state += ", LOCKED" if r.locked else ""
                last = f"{r.last_login_at:%d %b %Y %H:%M}" if r.last_login_at else "never"
                print(f"{r.username:<16} {r.display_name:<24} {state:<16} last sign-in {last:<18} sessions {r.sessions}")
        elif args.cmd == "add":
            username = args.username.strip()
            if not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
                sys.exit("Use letters, digits, '.', '_' or '-' in the username.")
            if conn.execute(text("SELECT 1 FROM auth.app_user WHERE lower(username) = lower(:u)"), {"u": username}).first():
                sys.exit(f"{username} already exists (use reset to change the password).")
            conn.execute(text("""INSERT INTO auth.app_user (username, display_name, password_hash)
                                 VALUES (:u, :n, :h)"""),
                         {"u": username, "n": args.name.strip(), "h": hash_password(_ask_password(username))})
            print(f"Added {username} ({args.name.strip()}).")
        elif args.cmd == "log":
            for r in conn.execute(text("""SELECT at AT TIME ZONE 'Asia/Kolkata' AS at, username, success, reason, ip
                                          FROM auth.login_event ORDER BY event_id DESC LIMIT :n"""), {"n": args.limit}):
                print(f"{r.at:%d %b %H:%M:%S}  {r.username:<16} {'ok  ' if r.success else 'FAIL'} {r.reason or '':<16} {r.ip or ''}")
        else:
            user = _user(conn, args.username)
            if args.cmd == "reset":
                conn.execute(text("""UPDATE auth.app_user SET password_hash = :h, password_changed_at = now(),
                                     failed_logins = 0, locked_until = NULL WHERE user_id = :id"""),
                             {"h": hash_password(_ask_password(user.username)), "id": user.user_id})
            elif args.cmd == "disable":
                conn.execute(text("UPDATE auth.app_user SET active = false WHERE user_id = :id"), {"id": user.user_id})
            elif args.cmd == "enable":
                conn.execute(text("""UPDATE auth.app_user SET active = true, failed_logins = 0, locked_until = NULL
                                     WHERE user_id = :id"""), {"id": user.user_id})
            if args.cmd in ("reset", "disable", "signout"):
                n = conn.execute(text("DELETE FROM auth.session WHERE user_id = :id"), {"id": user.user_id}).rowcount
                print(f"{args.cmd}: {user.username} done; {n} session(s) ended.")
            else:
                print(f"{user.username} enabled.")


if __name__ == "__main__":
    main()
