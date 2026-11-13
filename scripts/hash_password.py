"""Optional: store a bcrypt hash instead of a plain ADMIN_PASSWORD.
    python scripts/hash_password.py
Put the output in .env as ADMIN_PASSWORD_HASH='...' (keep the single quotes: the hash contains $ characters)."""
import getpass
import sys

import bcrypt

pw = getpass.getpass("New admin password: ")
if len(pw) < 12:
    sys.exit("Use at least 12 characters.")
print(bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(12)).decode())
