"""
Admin commands, run on the server.

    python manage.py add-user <login-id> --name "Full Name" --role admin|editor|viewer
    python manage.py reset-password <login-id>
    python manage.py list-users
    python manage.py backup <path/to/backup.db>

add-user and reset-password ask for the password (it isn't shown as you type).
Add --generate to create a random temporary password instead and print it.
Create the first admin here; after that, admins can manage users in the dashboard's Users tab.
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

import auth
import db


def _password(generate: bool) -> str:
    if generate:
        pw = secrets.token_urlsafe(9)
        print(f"Temporary password: {pw}\n(Ask the person to change it after signing in: their name > Change password.)")
        return pw
    while True:
        pw = getpass.getpass("Password (min 8 characters): ")
        try:
            auth.check_password(pw)
        except ValueError as e:
            print(e)
            continue
        if getpass.getpass("Repeat password: ") == pw:
            return pw
        print("The passwords don't match. Try again.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Project tracker admin commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user", help="create a login")
    a.add_argument("username")
    a.add_argument("--name", required=True)
    a.add_argument("--role", default="editor", choices=auth.ROLES)
    a.add_argument("--generate", action="store_true", help="generate a temporary password")
    r = sub.add_parser("reset-password", help="set a new password (signs the person out everywhere)")
    r.add_argument("username")
    r.add_argument("--generate", action="store_true")
    sub.add_parser("list-users")
    b = sub.add_parser("backup", help="copy the database (safe while the server is running)")
    b.add_argument("dest")
    args = ap.parse_args(argv)

    db.init()
    try:
        if args.cmd == "add-user":
            username = auth.check_username(args.username)
            if db.get_user(username):
                sys.exit(f"'{username}' already exists. Use reset-password to change the password.")
            u = db.create_user(username, args.name.strip(), args.role,
                               auth.hash_password(_password(args.generate)), "command line")
            print(f"Created {u['username']} ({u['name']}, {u['role']}).")
        elif args.cmd == "reset-password":
            username = auth.check_username(args.username)
            if not db.get_user(username):
                sys.exit(f"No user '{username}'.")
            db.update_user(username, "command line", password_hash=auth.hash_password(_password(args.generate)))
            print(f"Password changed for {username}. They have been signed out everywhere.")
        elif args.cmd == "list-users":
            users = db.list_users()
            if not users:
                print("No users yet. Create the first admin:\n"
                      '  python manage.py add-user admin --name "Your Name" --role admin')
            for u in users:
                print(f"{u['username']:<20} {u['role']:<7} {'active' if u['active'] else 'DEACTIVATED':<12} "
                      f"{u['name']}  (last login: {u['last_login'] or 'never'})")
        elif args.cmd == "backup":
            db.backup(args.dest)
            print("Backed up to", args.dest)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
