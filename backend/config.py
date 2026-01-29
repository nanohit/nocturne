import os
from dotenv import load_dotenv

load_dotenv()

HDREZKA_EMAIL = os.getenv("HDREZKA_EMAIL")
HDREZKA_PASSWORD = os.getenv("HDREZKA_PASSWORD")
# Default to hdrezka.me - it has working CDN (not voidboost.cc which 404s)
HDREZKA_MIRROR = os.getenv("HDREZKA_MIRROR", "https://hdrezka.me/")
