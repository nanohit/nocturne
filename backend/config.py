import os
from dotenv import load_dotenv

load_dotenv()

HDREZKA_EMAIL = os.getenv("HDREZKA_EMAIL")
HDREZKA_PASSWORD = os.getenv("HDREZKA_PASSWORD")
HDREZKA_MIRROR = os.getenv("HDREZKA_MIRROR", "https://rezka.fi/")
