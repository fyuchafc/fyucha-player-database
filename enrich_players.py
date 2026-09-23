import json
import time
import hashlib
import re
import unicodedata
from pathlib import Path
from datetime import datetime

import requests


# ============================================================
# Fyucha Player Database
# Wikidata Enrichment V2.1
# ============================================================

INPUT_FILE = Path("output/players.json")

TEST_OUTPUT_FILE = Path("output/enriched-players-test.json")
MATCH_OUTPUT_FILE = Path("output/wikidata-matches-test.json")

# NEW IN V2.1
CORRECTIONS_FILE = Path("output/dob-corrections-test.json")
YEAR_ONLY_FILE = Path("output/year-only-test.json")

# Test only the first 100 records for now.
TEST_LIMIT = 100

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

USER_AGENT = (
    "FyuchaPlayerDatabase/2.1 "
    "(https://github.com/fyucha/fyucha-player-database)"
)

REQUEST_DELAY = 0.5

MAX_RETRIES = 5


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT
})


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_text(value):
    if not value:
        return ""

    value = str(value)

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()

    value = value.replace("-", " ")
    value = value.replace("_", " ")

    value = re.sub(r"[^a-z0-9\s]", " ", value)

    value = re.sub(r"\s+", " ", value).strip()

    return value


def normalize_name(value):
    return normalize_text(value)


# ============================================================
# DATE HELPERS
# ============================================================

def is_valid_date(value):
    if not value:
        return False

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def get_date_precision(value):
    """
    OpenFootball currently represents some year-only dates
    as YYYY-01-01.

    V2.1 treats these as year-only records.
    """

    if not value:
        return None

    if re.fullmatch(r"\d{4}-01-01", value):
        return "year"

    if re.fullmatch(r"\d{4}-\d{2}-01", value):
        return "month"

    if is_valid_date(value):
        return "day"

    return None


def extract_year(value):
    if not value:
        return None

    match = re.match(r"^(\d{4})", str(value))

    if match:
        return int(match.group(1))

    return None


# ============================================================
# WIKIDATA API
# ============================================================

def wikidata_search(name):
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "format": "json",
        "limit": 10,
        "type": "item"
    }

    for attempt in range(MAX_RETRIES):

        try:
            response = session.get(
                WIKIDATA_API,
                params=params,
                timeout=30
            )

            if response.status_code == 429:
                wait = 2 ** attempt
                print(
                    f"Wikidata rate limit. Waiting {wait}s..."
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = 2 ** attempt
                print(
                    f"Wikidata server error "
                    f"{response.status_code}. Waiting {wait}s..."
                )
                time.sleep(wait)
                continue

            response.raise_for_status()

            return response.json().get("search", [])

        except requests.RequestException as exc:

            wait = 2 ** attempt

            print(
                f"Wikidata search error: {exc}. "
                f"Waiting {wait}s..."
            )

            time.sleep(wait)

    return []


def get_entity(entity_id):

    params = {
        "action": "wbgetentities",
        "ids": entity_id,
        "format": "json",
        "languages": "en",
        "props": "claims|descriptions|labels"
    }

    for attempt in range(MAX_RETRIES):

        try:

            response = session.get(
                WIKIDATA_API,
                params=params,
                timeout=30
            )

            if response.status_code == 429:
                wait = 2 ** attempt
                print(
                    f"Wikidata rate limit. Waiting {wait}s..."
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = 2 ** attempt
                print(
                    f"Wikidata server error "
                    f"{response.status_code}. Waiting {wait}s..."
                )
                time.sleep(wait)
                continue

            response.raise_for_status()

            data = response.json()

            return data.get("entities", {}).get(entity_id)

        except requests.RequestException as exc:

            wait = 2 ** attempt

            print(
                f"Wikidata entity error: {exc}. "
                f"Waiting {wait}s..."
            )

            time.sleep(wait)

    return None


# ============================================================
# CLAIM HELPERS
# ============================================================

def get_claim_time(entity, property_id):

    claims = entity.get("claims", {}).get(property_id, [])

    if not claims:
        return None

    claim = claims[0]

    mainsnak = claim.get("mainsnak", {})

    datavalue = mainsnak.get("datavalue", {})

    value = datavalue.get("value", {})

    if not isinstance(value, dict):
        return None

    time_value = value.get("time")

    if not time_value:
        return None

    # Example:
    # +1993-09-05T00:00:00Z

    match = re.match(
        r"^\+(\d{4})-(\d{2})-(\d{2})",
        time_value
    )

    if not match:
        return None

    year = match.group(1)
    month = match.group(2)
    day = match.group(3)

    precision = value.get("precision")

    if precision == 9:
        return {
            "date": f"{year}-01-01",
            "precision": "year"
        }

    if precision == 10:
        return {
            "date": f"{year}-{month}-01",
            "precision": "month"
        }

    if precision == 11:
        return {
            "date": f"{year}-{month}-{day}",
            "precision": "day"
        }

    return {
        "date": f"{year}-{month}-{day}",
        "precision": "unknown"
    }


def get_claim_string(entity, property_id):

    claims = entity.get("claims", {}).get(property_id, [])

    if not claims:
        return None

    claim = claims[0]

    mainsnak = claim.get("mainsnak", {})

    datavalue = mainsnak.get("datavalue", {})

    value = datavalue.get("value")

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return value.get("id")

    return None


def get_claim_ids(entity, property_id):

    claims = entity.get("claims", {}).get(property_id, [])

    result = []

    for claim in claims:

        mainsnak = claim.get("mainsnak", {})

        datavalue = mainsnak.get("datavalue", {})

        value = datavalue.get("value")

        if isinstance(value, dict):

            entity_id = value.get("id")

            if entity_id:
                result.append(entity_id)

    return result


# ============================================================
# WIKIDATA INFORMATION
# ============================================================

def extract_wikidata_info(entity):

    if not entity:
        return None

    label = (
        entity.get("labels", {})
        .get("en", {})
        .get("value")
    )

    description = (
        entity.get("descriptions", {})
        .get("en", {})
        .get("value")
    )

    dob = get_claim_time(entity, "P569")

    dod = get_claim_time(entity, "P570")

    birth_place = get_claim_string(entity, "P19")

    citizenship = get_claim_ids(entity, "P27")

    occupations = get_claim_ids(entity, "P106")

    image = get_claim_string(entity, "P18")

    return {
        "wikidataId": entity.get("id"),
        "label": label,
        "description": description,
        "dateOfBirth": dob,
        "dateOfDeath": dod,
        "birthPlace": birth_place,
        "citizenship": citizenship,
        "occupations": occupations,
        "image": image
    }


# ============================================================
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_OCCUPATIONS = {
    "Q937857",
    "Q10833314",
    "Q4610556",
    "Q628099",
}


def football_relevance(info):

    score = 0

    occupations = set(info.get("occupations", []))

    if occupations.intersection(FOOTBALL_OCCUPATIONS):
        score += 5

    description = (
        info.get("description") or ""
    ).lower()

    football_terms = [
        "footballer",
        "football player",
        "soccer player",
        "football manager",
        "football coach",
        "soccer player",
    ]

    for term in football_terms:

        if term in description:
            score += 5
            break

    return score


# ============================================================
# MATCHING
# ============================================================

def calculate_match_score(player, info):

    player_name = normalize_name(
        player.get("name")
        or player.get("displayName")
    )

    wiki_name = normalize_name(
        info.get("label")
    )

    score = 0

    # Exact normalized name
    if player_name and wiki_name == player_name:
        score += 10

    # Partial name similarity
    elif player_name and wiki_name:

        player_parts = set(player_name.split())
        wiki_parts = set(wiki_name.split())

        overlap = player_parts.intersection(wiki_parts)

        if overlap:
            score += min(6, len(overlap) * 3)

    # DOB matching
    source_dob = player.get("dateOfBirth")
    source_precision = get_date_precision(source_dob)

    wiki_dob = info.get("dateOfBirth")

    if source_dob and wiki_dob:

        if source_precision == "day":

            if wiki_dob["precision"] == "day":

                if source_dob == wiki_dob["date"]:
                    score += 20
                else:
                    # Do not accept conflicting full dates
                    score -= 20

        elif source_precision == "year":

            source_year = extract_year(source_dob)

            wiki_year = extract_year(
                wiki_dob["date"]
            )

            if source_year == wiki_year:
                score += 12

    # Football relevance
    score += football_relevance(info)

    return score


def determine_match_method(player, info):

    source_dob = player.get("dateOfBirth")

    wiki_dob = info.get("dateOfBirth")

    source_precision = get_date_precision(source_dob)

    if not source_dob or not wiki_dob:
        return "name-only"

    if source_precision == "year":

        if wiki_dob["precision"] == "day":

            return "year-to-full-dob"

        if wiki_dob["precision"] == "month":

            return "year-to-month-dob"

        return "year-only"

    if source_precision == "day":

        if wiki_dob["precision"] == "day":

            if source_dob == wiki_dob["date"]:
                return "full-dob-exact"

            return "name-conflict-dob"

        return "full-dob-to-lower-precision"

    return "unknown"


# ============================================================
# ENRICH PLAYER
# ============================================================

def enrich_player(player):

    name = (
        player.get("name")
        or player.get("displayName")
    )

    print(f"Searching Wikidata: {name}")

    candidates = wikidata_search(name)

    time.sleep(REQUEST_DELAY)

    if not candidates:

        return None, {
            "name": name,
            "status": "not-found"
        }

    best = None
    best_score = -999

    candidate_debug = []

    for candidate in candidates[:10]:

        entity_id = candidate.get("id")

        if not entity_id:
            continue

        entity = get_entity(entity_id)

        time.sleep(REQUEST_DELAY)

        if not entity:
            continue

        info = extract_wikidata_info(entity)

        if not info:
            continue

        score = calculate_match_score(
            player,
            info
        )

        candidate_debug.append({
            "wikidataId": entity_id,
            "label": info.get("label"),
            "description": info.get("description"),
            "score": score
        })

        if score > best_score:

            best_score = score
            best = info

    # Conservative threshold
    if not best or best_score < 10:

        return None, {
            "name": name,
            "status": "no-confident-match",
            "bestScore": best_score,
            "candidates": candidate_debug
        }

    method = determine_match_method(
        player,
        best
    )

    result = dict(player)

    source_dob = player.get("dateOfBirth")

    wiki_dob = best.get("dateOfBirth")

    source_precision = get_date_precision(
        source_dob
    )

    # --------------------------------------------------------
    # Wikidata metadata
    # --------------------------------------------------------

    result["wikidata"] = {
        "id": best.get("wikidataId"),
        "label": best.get("label"),
        "description": best.get("description"),
        "dateOfBirth": (
            best["dateOfBirth"]["date"]
            if best.get("dateOfBirth")
            else None
        ),
        "dateOfBirthPrecision": (
            best["dateOfBirth"]["precision"]
            if best.get("dateOfBirth")
            else None
        ),
        "dateOfDeath": (
            best["dateOfDeath"]["date"]
            if best.get("dateOfDeath")
            else None
        ),
        "dateOfDeathPrecision": (
            best["dateOfDeath"]["precision"]
            if best.get("dateOfDeath")
            else None
        ),
        "birthPlace": best.get("birthPlace"),
        "citizenship": best.get("citizenship", []),
        "occupations": best.get("occupations", []),
        "image": best.get("image")
    }

    result["matchScore"] = best_score

    result["matchMethod"] = method

    # --------------------------------------------------------
    # DOB precision handling
    # --------------------------------------------------------

    if wiki_dob:

        wiki_date = wiki_dob["date"]
        wiki_precision = wiki_dob["precision"]

        # Year-only OpenFootball record
        # can be upgraded by Wikidata.
        if source_precision == "year":

            if wiki_precision == "day":

                result["dateOfBirth"] = wiki_date

                result["dateOfBirthPrecision"] = "day"

                result["birthDay"] = wiki_date[5:]

            elif wiki_precision == "month":

                result["dateOfBirth"] = wiki_date

                result["dateOfBirthPrecision"] = "month"

                result["birthDay"] = None

            else:

                result["dateOfBirthPrecision"] = "year"

                result["birthDay"] = None

        # Full OpenFootball date
        else:

            result["dateOfBirthPrecision"] = (
                source_precision
            )

            if source_precision == "day":
                result["birthDay"] = source_dob[5:]

    # --------------------------------------------------------
    # Deceased status
    # --------------------------------------------------------

    if best.get("dateOfDeath"):

        result["careerStatus"] = "deceased"

    return result, {
        "name": name,
        "status": "matched",
        "wikidataId": best.get("wikidataId"),
        "wikidataLabel": best.get("label"),
        "matchScore": best_score,
        "matchMethod": method,
        "sourceDateOfBirth": source_dob,
        "sourceDatePrecision": source_precision,
        "wikidataDateOfBirth": (
            wiki_dob["date"]
            if wiki_dob
            else None
        ),
        "wikidataDatePrecision": (
            wiki_dob["precision"]
            if wiki_dob
            else None
        )
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print("==============================================")
    print("Fyucha Player Database - Wikidata V2.1")
    print("==============================================")

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE}"
        )

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        players = json.load(f)

    test_players = players[:TEST_LIMIT]

    print(
        f"Loaded {len(players):,} total players."
    )

    print(
        f"Testing first {len(test_players)} players."
    )

    enriched_players = []

    match_records = []

    correction_records = []

    year_only_records = []

    matched = 0
    not_matched = 0
    errors = 0

    full_dates = 0
    month_dates = 0
    year_dates = 0

    corrected_dates = 0
    deceased_records = 0

    for index, player in enumerate(
        test_players,
        start=1
    ):

        print(
            f"\n[{index}/{len(test_players)}]"
        )

        try:

            enriched, match_info = enrich_player(
                player
            )

            if enriched:

                enriched_players.append(
                    enriched
                )

                matched += 1

                # ------------------------------------------------
                # Precision counters
                # ------------------------------------------------

                precision = enriched.get(
                    "dateOfBirthPrecision"
                )

                if precision == "day":
                    full_dates += 1

                elif precision == "month":
                    month_dates += 1

                elif precision == "year":
                    year_dates += 1

                # ------------------------------------------------
                # Actual DOB correction
                # ------------------------------------------------

                source_dob = (
                    match_info.get(
                        "sourceDateOfBirth"
                    )
                )

                wiki_dob = (
                    match_info.get(
                        "wikidataDateOfBirth"
                    )
                )

                method = (
                    match_info.get(
                        "matchMethod"
                    )
                )

                if (
                    source_dob
                    and wiki_dob
                    and source_dob != wiki_dob
                    and method in {
                        "year-to-full-dob",
                        "year-to-month-dob"
                    }
                ):

                    corrected_dates += 1

                    correction_records.append({
                        "id": enriched.get("id"),
                        "name": enriched.get(
                            "name"
                        ),
                        "displayName": enriched.get(
                            "displayName"
                        ),
                        "sourceDateOfBirth": source_dob,
                        "sourceDatePrecision": (
                            match_info.get(
                                "sourceDatePrecision"
                            )
                        ),
                        "correctedDateOfBirth": wiki_dob,
                        "correctedDatePrecision": (
                            match_info.get(
                                "wikidataDatePrecision"
                            )
                        ),
                        "matchMethod": method,
                        "matchScore": (
                            match_info.get(
                                "matchScore"
                            )
                        ),
                        "wikidataId": (
                            match_info.get(
                                "wikidataId"
                            )
                        )
                    })

                # ------------------------------------------------
                # Year-only records
                # ------------------------------------------------

                if (
                    enriched.get(
                        "dateOfBirthPrecision"
                    ) == "year"
                ):

                    year_only_records.append({
                        "id": enriched.get("id"),
                        "name": enriched.get(
                            "name"
                        ),
                        "dateOfBirth": enriched.get(
                            "dateOfBirth"
                        ),
                        "dateOfBirthPrecision": (
                            enriched.get(
                                "dateOfBirthPrecision"
                            )
                        ),
                        "matchMethod": (
                            enriched.get(
                                "matchMethod"
                            )
                        ),
                        "wikidataId": (
                            enriched.get(
                                "wikidata",
                                {}
                            ).get("id")
                        )
                    })

                if (
                    enriched.get(
                        "careerStatus"
                    ) == "deceased"
                ):

                    deceased_records += 1

            else:

                not_matched += 1

                # ------------------------------------------------
                # Keep unmatched year-only records
                # ------------------------------------------------

                source_dob = player.get(
                    "dateOfBirth"
                )

                if (
                    get_date_precision(
                        source_dob
                    ) == "year"
                ):

                    year_only_records.append({
                        "id": player.get("id"),
                        "name": player.get(
                            "name"
                        ),
                        "dateOfBirth": source_dob,
                        "dateOfBirthPrecision": "year",
                        "matchMethod": None,
                        "wikidataId": None
                    })

            match_records.append(
                match_info
            )

        except Exception as exc:

            errors += 1

            print(
                f"ERROR: {exc}"
            )

            match_records.append({
                "name": player.get(
                    "name"
                ),
                "status": "error",
                "error": str(exc)
            })

    # ========================================================
    # SAVE MAIN TEST OUTPUT
    # ========================================================

    with open(
        TEST_OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            enriched_players,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # SAVE MATCH LOG
    # ========================================================

    with open(
        MATCH_OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            match_records,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # SAVE DOB CORRECTIONS
    # ========================================================

    with open(
        CORRECTIONS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            correction_records,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # SAVE YEAR-ONLY RECORDS
    # ========================================================

    with open(
        YEAR_ONLY_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            year_only_records,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # SUMMARY
    # ========================================================

    print("\n==============================================")
    print("V2.1 TEST COMPLETE")
    print("==============================================")

    print(
        f"Players tested:        {len(test_players)}"
    )

    print(
        f"Matched:               {matched}"
    )

    print(
        f"Not matched:           {not_matched}"
    )

    print(
        f"Errors:                {errors}"
    )

    print(
        f"Full DOB records:      {full_dates}"
    )

    print(
        f"Month precision:       {month_dates}"
    )

    print(
        f"Year precision:        {year_dates}"
    )

    print(
        f"DOB corrections:       {corrected_dates}"
    )

    print(
        f"Year-only records:     {len(year_only_records)}"
    )

    print(
        f"Deceased records:      {deceased_records}"
    )

    print("\nFiles created:")

    print(
        f" - {TEST_OUTPUT_FILE}"
    )

    print(
        f" - {MATCH_OUTPUT_FILE}"
    )

    print(
        f" - {CORRECTIONS_FILE}"
    )

    print(
        f" - {YEAR_ONLY_FILE}"
    )

    print("==============================================")


if __name__ == "__main__":
    main()
