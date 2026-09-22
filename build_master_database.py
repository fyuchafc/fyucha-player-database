import json
import os
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


# ============================================================
# FYUCHA FOOTBALL PLAYER MASTER DATABASE
# ============================================================

GITHUB_API = "https://api.github.com/repos/openfootball/players"
RAW_BASE = "https://raw.githubusercontent.com/openfootball/players/master/"

OUTPUT_DIR = Path("output")


# ------------------------------------------------------------
# HTTP HELPERS
# ------------------------------------------------------------

def get_json(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Fyucha-Player-Database"
    }

    token = os.environ.get("GITHUB_TOKEN")

    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers)

    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url):
    headers = {
        "User-Agent": "Fyucha-Player-Database"
    }

    request = Request(url, headers=headers)

    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


# ------------------------------------------------------------
# FIND ALL PLAYER FILES
# ------------------------------------------------------------

def find_player_files():
    print("Finding OpenFootball player files...")

    tree_url = (
        GITHUB_API
        + "/git/trees/master?recursive=1"
    )

    data = get_json(tree_url)

    files = []

    for item in data.get("tree", []):

        if item.get("type") != "blob":
            continue

        path = item.get("path", "")

        if path.endswith(".players.txt"):
            files.append(path)

    files.sort()

    print(f"Found {len(files)} player files.")

    return files


# ------------------------------------------------------------
# POSITION NORMALIZATION
# ------------------------------------------------------------

def normalize_position(position):

    position = position.strip().upper()

    mapping = {
        "G": "Goalkeeper",
        "GK": "Goalkeeper",

        "D": "Defender",
        "DF": "Defender",

        "M": "Midfielder",
        "MF": "Midfielder",

        "F": "Forward",
        "FW": "Forward"
    }

    return mapping.get(position, None)


# ------------------------------------------------------------
# DATE PARSER
# ------------------------------------------------------------

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12
}


def make_date(day, month, year):

    try:

        month_number = MONTHS[month[:3].title()]

        date_object = datetime(
            int(year),
            month_number,
            int(day)
        )

        return date_object.strftime("%Y-%m-%d")

    except Exception:
        return None


# ------------------------------------------------------------
# NORMALIZE PLAYER NAME
# ------------------------------------------------------------

def normalize_name(name):

    name = re.sub(r"\s+", " ", name)

    return name.strip()


# ------------------------------------------------------------
# CREATE PLAYER ID
# ------------------------------------------------------------

def create_player_id(name, date_of_birth):

    value = f"{name.lower()}|{date_of_birth}"

    digest = hashlib.sha1(
        value.encode("utf-8")
    ).hexdigest()

    return "fy-" + digest[:16]


# ------------------------------------------------------------
# PARSE PLAYER LINE
# ------------------------------------------------------------

PLAYER_PATTERN = re.compile(
    r"""
    ^\s*
    (.+?)
    \s*,\s*
    ([A-Z]+)
    \s*,\s*
    ([^,]*)
    \s*,\s*
    b\.\s+
    (\d{1,2})
    \s+
    ([A-Za-z]{3})
    \s+
    (\d{4})
    (?:\s+@\s+(.+?))?
    \s*$
    """,
    re.VERBOSE
)


def parse_player_line(line):

    match = PLAYER_PATTERN.match(line)

    if not match:
        return None

    name = normalize_name(match.group(1))

    position = normalize_position(
        match.group(2)
    )

    height = match.group(3).strip()

    day = match.group(4)
    month = match.group(5)
    year = match.group(6)

    birthplace = match.group(7)

    date_of_birth = make_date(
        day,
        month,
        year
    )

    if not date_of_birth:
        return None

    return {
        "name": name,
        "position": position,
        "height": height if height != "-" else None,
        "dateOfBirth": date_of_birth,
        "birthPlace": birthplace.strip()
        if birthplace else None
    }


# ------------------------------------------------------------
# COUNTRY FROM FILE PATH
# ------------------------------------------------------------

def country_from_path(path):

    parts = path.split("/")

    if len(parts) >= 3:

        country = parts[-2]

        return country.replace("-", " ").title()

    return None


# ------------------------------------------------------------
# ADD / MERGE PLAYER
# ------------------------------------------------------------

def add_player(database, player, source_file):

    name = player["name"]

    date_of_birth = player["dateOfBirth"]

    key = (
        name.lower(),
        date_of_birth
    )

    represented_country = country_from_path(
        source_file
    )

    if key not in database:

        database[key] = {

            "id": create_player_id(
                name,
                date_of_birth
            ),

            "name": name,

            "displayName": name,

            "dateOfBirth": date_of_birth,

            "birthDay": date_of_birth[5:],

            "birthYear": int(
                date_of_birth[:4]
            ),

            "birthPlace": player["birthPlace"],

            "nationality": None,

            "representedCountry": represented_country,

            "representedCountries": (
                [represented_country]
                if represented_country
                else []
            ),

            "position": (
                [player["position"]]
                if player["position"]
                else []
            ),

            "height": player["height"],

            "gender": None,

            "careerStatus": "unknown",

            "currentClub": None,

            "formerClubs": [],

            "photo": None,

            "aliases": [],

            "sources": {

                "openfootball": {
                    "file": source_file
                },

                "wikidata": None,

                "sportmonks": None,

                "apiFootball": None
            },

            "lastUpdated": datetime.now(
                timezone.utc
            ).isoformat()
        }

        return

    # --------------------------------------------------------
    # MERGE DUPLICATE RECORD
    # --------------------------------------------------------

    existing = database[key]

    if player["position"]:

        if player["position"] not in existing["position"]:

            existing["position"].append(
                player["position"]
            )

    if represented_country:

        if represented_country not in existing[
            "representedCountries"
        ]:

            existing[
                "representedCountries"
            ].append(
                represented_country
            )

    if not existing["birthPlace"]:

        existing["birthPlace"] = player[
            "birthPlace"
        ]

    if not existing["height"]:

        existing["height"] = player[
            "height"
        ]


# ------------------------------------------------------------
# BUILD DATABASE
# ------------------------------------------------------------

def build_database():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    player_files = find_player_files()

    database = {}

    total_lines = 0

    successful_records = 0

    for index, file_path in enumerate(
        player_files,
        start=1
    ):

        print(
            f"[{index}/{len(player_files)}] "
            f"Reading {file_path}"
        )

        url = RAW_BASE + file_path

        try:

            content = get_text(url)

        except Exception as error:

            print(
                f"WARNING: Could not download "
                f"{file_path}: {error}"
            )

            continue

        for line in content.splitlines():

            total_lines += 1

            line = line.strip()

            if not line:
                continue

            if line.startswith("="):
                continue

            if line.startswith("#"):
                continue

            player = parse_player_line(
                line
            )

            if not player:
                continue

            add_player(
                database,
                player,
                file_path
            )

            successful_records += 1

    players = list(
        database.values()
    )

    players.sort(
        key=lambda player: (
            player["birthDay"],
            player["name"].lower()
        )
    )

    # --------------------------------------------------------
    # PLAYERS.JSON
    # --------------------------------------------------------

    players_file = (
        OUTPUT_DIR / "players.json"
    )

    with players_file.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            players,
            file,
            ensure_ascii=False,
            indent=2
        )

    # --------------------------------------------------------
    # BIRTHDAY INDEX
    # --------------------------------------------------------

    birthday_index = {}

    for player in players:

        birthday = player["birthDay"]

        if birthday not in birthday_index:

            birthday_index[birthday] = []

        birthday_index[birthday].append(
            player["id"]
        )

    birthday_file = (
        OUTPUT_DIR /
        "birthday-index.json"
    )

    with birthday_file.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            birthday_index,
            file,
            ensure_ascii=False,
            indent=2
        )

    # --------------------------------------------------------
    # DATABASE METADATA
    # --------------------------------------------------------

    metadata = {

        "database": "Fyucha Football Player Master Database",

        "version": "1.0.0",

        "generatedAt": datetime.now(
            timezone.utc
        ).isoformat(),

        "source": {

            "name": "OpenFootball Players",

            "repository":
                "https://github.com/openfootball/players",

            "license": "CC0-1.0"
        },

        "statistics": {

            "playerRecords": len(players),

            "sourceFiles": len(player_files),

            "parsedPlayerLines":
                successful_records,

            "scannedLines":
                total_lines
        },

        "statusPolicy": {

            "active":
                "Will be populated during enrichment",

            "retired":
                "Will be populated during enrichment",

            "unknown":
                "Used when retirement status has not been verified"
        }
    }

    metadata_file = (
        OUTPUT_DIR /
        "database-meta.json"
    )

    with metadata_file.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            metadata,
            file,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 60)
    print("FYUCHA PLAYER DATABASE BUILD COMPLETE")
    print("=" * 60)
    print(
        f"Players: {len(players):,}"
    )
    print(
        f"Source files: {len(player_files):,}"
    )
    print(
        f"Output folder: {OUTPUT_DIR}"
    )
    print("=" * 60)


if __name__ == "__main__":

    build_database()
