"""Runtime configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RECON_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("RECON_DB_PATH", DATA_DIR / "recon.sqlite3"))

# Google OAuth client file downloaded from Google Cloud Console (Desktop app type).
GOOGLE_CLIENT_SECRETS = Path(os.environ.get("GOOGLE_CLIENT_SECRETS", DATA_DIR / "credentials.json"))
# Where the user's refresh token is stored after the first login.
GOOGLE_TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKEN_PATH", DATA_DIR / "token.json"))
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Gmail search used to find Amazon order / shipment / refund emails.
GMAIL_QUERY = os.environ.get(
    "RECON_GMAIL_QUERY",
    "from:(amazon.ca OR amazon.com OR amazon.co.uk OR amazon.de OR amazon.fr OR amazon.com.au) "
    "-from:aws -from:no-reply-aws@amazon.com "
    "subject:(Ordered OR Shipped OR order OR shipped OR refund OR Refund OR invoice)",
)
# How far back the first Gmail sync looks.
GMAIL_INITIAL_LOOKBACK_DAYS = int(os.environ.get("RECON_GMAIL_LOOKBACK_DAYS", "400"))

# Matching parameters (days). Amazon charges when an item ships, so the card
# charge normally lands between the ship date and a few days after it.
MATCH_SHIPMENT_WINDOW_BEFORE = int(os.environ.get("RECON_WINDOW_BEFORE", "3"))
MATCH_SHIPMENT_WINDOW_AFTER = int(os.environ.get("RECON_WINDOW_AFTER", "10"))
MATCH_ORDER_WINDOW_AFTER = int(os.environ.get("RECON_ORDER_WINDOW_AFTER", "45"))
MATCH_AMOUNT_TOLERANCE = float(os.environ.get("RECON_AMOUNT_TOLERANCE", "0.00"))
# Regex applied to the bank description to decide whether a line is an Amazon charge.
AMAZON_DESCRIPTION_REGEX = os.environ.get(
    "RECON_AMAZON_REGEX", r"AMAZON|AMZN|AMZ\b|PRIME\s*VIDEO|PRIME\s*MEMBER"
)
