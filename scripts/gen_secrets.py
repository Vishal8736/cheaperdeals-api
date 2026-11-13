"""Print fresh secrets for .env. Works on Windows: python scripts/gen_secrets.py"""
import secrets
import string

alnum = string.ascii_letters + string.digits
print("SECRET_KEY=" + secrets.token_urlsafe(48))
print("INTERNAL_API_KEY=" + secrets.token_urlsafe(48))
print("N8N_ENCRYPTION_KEY=" + secrets.token_urlsafe(48))
print("POSTGRES_PASSWORD=" + "".join(secrets.choice(alnum) for _ in range(28)))
print("ADMIN_PASSWORD=" + "".join(secrets.choice(alnum) for _ in range(20)))
