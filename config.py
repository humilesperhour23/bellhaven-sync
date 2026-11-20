"""All tunable settings for the Bellhaven sync, in one place.

Change a value here and re-run match.py. Nothing else needs editing.
"""

# --- Applying changes ------------------------------------------------------------
# True  = the review app only SHOWS the exact API requests it would send.
# False = Approve really writes to the CRM.
DRY_RUN = False

# Every note the sync writes starts with "[YYYY-MM-DD] <NOTE_PREFIX>: ..."
NOTE_PREFIX = "Bellhaven sync"

# --- Scheduled run safety --------------------------------------------------------
# run_pipeline.py stops (non-zero exit, matcher not run, old website data kept)
# if the scrape finds fewer communities than this. Protects against a broken
# website or layout change making every account look "not on website".
MIN_EXPECTED_LOCATIONS = 30

# --- Where things live -------------------------------------------------------
SITE_URL = "https://analyst-assessment-production.up.railway.app"
API_BASE = "https://analyst-assessment-production.up.railway.app/api/v1"
WEBSITE_FILE = "data/website_locations.json"
DB_PATH = "data/pipeline.db"

# --- The Bellhaven parent account ---------------------------------------------
# Looked up via GET /accounts: the account named
# "Bellhaven Senior Living (Parent Account)". match.py checks this on every run
# and stops if the id no longer points to an account with this name.
BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"
BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# --- Matching thresholds (0 = anything goes, 1 = must be identical) ----------
# Name similarity needed to match a location to an account in the same
# city + state when the street address does not line up.
NAME_SIMILARITY_THRESHOLD = 0.85

# Street similarity needed to count two addresses as "near-identical" when the
# house number and zip are the same (catches typos like "Wilmington Pk" vs "Pike").
STREET_SIMILARITY_THRESHOLD = 0.85

# --- Address normalization -----------------------------------------------------
# Each word on the left is replaced by the word on the right before comparing,
# so "1250 Northwest Franklin Street" and "1250 NW Franklin St" become equal.
ADDRESS_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "court": "ct", "place": "pl",
    "parkway": "pkwy", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "square": "sq", "trail": "trl", "way": "way", "suite": "ste",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

# --- Name normalization (used only for comparing names, never written) ---------
NAME_WORD_MAP = {
    "centre": "center",
    "healthcare": "health care",
    "rehab": "rehabilitation",
    "the": "",
}

# --- Care types: website label -> CRM care_type value ---------------------------
CARE_TYPE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Independent Living": "Independent Living",
}

# --- What an UPDATE proposal is allowed to fix ---------------------------------
# Name and parent are always checked. Turn these off to ignore other differences.
FIX_ADDRESS = True     # street / city / state / zip differ from the website
FIX_CARE_TYPE = True   # CRM care_type is not one of the website's offerings
