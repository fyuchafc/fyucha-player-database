import json
import time
import re
import unicodedata
from pathlib import Path
from datetime import datetime

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2
# ============================================================
#
# IMPORTANT V2.2 SAFETY RULE:
#
# If the source only knows the birth YEAR, Wikidata may only
# upgrade the record to a full DOB when the Wikidata birth
# year EXACTLY matches the source birth year.
#
# Example:
#   Source:   1993
#   Wikidata: 1993-03-07  -> ACCEPT
#   Wikidata: 1994-10-21  -> REJECT + CONFLICT
#
# This prevents incorrect cross-year player matches.
# ============================================================


INPUT_FILE = Path("output/players.json")

ENRICHED_FILE = Path("output/enriched-players-test.json")
MATCHES_FILE = Path("output/wikidata-matches-test.json")
CORRECTIONS_FILE = Path("output/dob-corrections-test.json")
CONFLICTS_FILE = Path("output/dob-conflicts-test.json")
YEAR_ONLY_FILE = Path("output/year-only-test.json")

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2 "
    "(football player database enrichment; contact via GitHub)"
)

REQUEST_DELAY = 0.25

TEST_LIMIT = 100


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

def normalize_name(name):
    if not name:
        return ""

    text = unicodedata.normalize("NFKD", str(name))

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = text.replace("’", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")

    text = re.sub(r"[^a-z0-9]+", " ", text)

    text = re.sub(r"\s+", " ", text).strip()

    return text


def normalize_compact(name):
    return normalize_name(name).replace(" ", "")


# ============================================================
# DATE HELPERS
# ============================================================

def parse_source_date(value):
    if not value:
        return None, None

    value = str(value).strip()

    # YYYY
    if re.fullmatch(r"\d{4}", value):
        return value, "year"

    # YYYY-MM
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value, "month"

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value, "day"

    return None, None


def get_year_from_date(value):
    if not value:
        return None

    match = re.match(r"^(\d{4})", str(value))

    if match:
        return int(match.group(1))

    return None


def wikidata_time_to_date(time_value, precision):
    if not time_value:
        return None, None

    # Wikidata normally returns:
    # +1993-03-07T00:00:00Z
    # +1993-00-00T00:00:00Z
    # +1993-01-00T00:00:00Z

    text = str(time_value).lstrip("+")

    match = re.match(
        r"(\d{4})-(\d{2})-(\d{2})",
        text
    )

    if not match:
        return None, None

    year = match.group(1)
    month = match.group(2)
    day = match.group(3)

    if precision == 9:
        return year, "year"

    if precision == 10:
        if month == "00":
            return year, "year"

        return f"{year}-{month}", "month"

    if precision >= 11:
        if month == "00" or day == "00":
            return year, "year"

        return f"{year}-{month}-{day}", "day"

    return year, "year"


# ============================================================
# WIKIDATA API
# ============================================================

def wikidata_search(name, limit=10):

    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "uselang": "en",
        "format": "json",
        "limit": limit
    }

    try:
        response = session.get(
            WIKIDATA_API,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        return data.get("search", [])

    except Exception as exc:
        print(f"Search error for {name}: {exc}")
        return []


def get_entity(qid):

    params = {
        "action": "wbgetentities",
        "ids": qid,
        "props": "claims|labels|descriptions",
        "languages": "en",
        "format": "json"
    }

    try:
        response = session.get(
            WIKIDATA_API,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        return data.get("entities", {}).get(qid)

    except Exception as exc:
        print(f"Entity error for {qid}: {exc}")
        return None


# ============================================================
# CLAIM HELPERS
# ============================================================

def get_claim_value(entity, property_id):

    claims = entity.get("claims", {})

    if property_id not in claims:
        return None

    for claim in claims[property_id]:

        mainsnak = claim.get("mainsnak", {})

        datavalue = mainsnak.get("datavalue")

        if not datavalue:
            continue

        value = datavalue.get("value")

        if value is not None:
            return value

    return None


def get_claim_values(entity, property_id):

    claims = entity.get("claims", {})

    results = []

    for claim in claims.get(property_id, []):

        mainsnak = claim.get("mainsnak", {})

        datavalue = mainsnak.get("datavalue")

        if not datavalue:
            continue

        value = datavalue.get("value")

        if value is not None:
            results.append(value)

    return results


# ============================================================
# EXTRACT WIKIDATA DOB
# ============================================================

def extract_wikidata_dob(entity):

    claims = entity.get("claims", {})

    dob_claims = claims.get("P569", [])

    if not dob_claims:
        return None, None

    best_date = None
    best_precision = None

    for claim in dob_claims:

        mainsnak = claim.get("mainsnak", {})

        datavalue = mainsnak.get("datavalue")

        if not datavalue:
            continue

        value = datavalue.get("value", {})

        time_value = value.get("time")
        precision = value.get("precision")

        date_value, date_precision = wikidata_time_to_date(
            time_value,
            precision
        )

        if not date_value:
            continue

        # Prefer day precision over month over year
        if best_date is None:
            best_date = date_value
            best_precision = date_precision

        elif date_precision == "day" and best_precision != "day":
            best_date = date_value
            best_precision = date_precision

        elif date_precision == "month" and best_precision == "year":
            best_date = date_value
            best_precision = date_precision

    return best_date, best_precision


# ============================================================
# ENTITY DESCRIPTION
# ============================================================

def get_entity_description(entity):

    descriptions = entity.get("descriptions", {})

    description = descriptions.get("en", {})

    return description.get("value", "")


# ============================================================
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_TERMS = [
    "football",
    "soccer",
    "footballer",
    "soccer player",
    "association football",
    "manager",
    "coach",
    "goalkeeper",
    "midfielder",
    "defender",
    "forward",
    "striker",
    "sports",
    "athlete"
]


def football_relevance(description):

    text = normalize_name(description)

    score = 0

    for term in FOOTBALL_TERMS:
        if normalize_name(term) in text:
            score += 3

    return score


# ============================================================
# NAME SCORING
# ============================================================

def name_similarity(source_name, wikidata_name):

    source = normalize_name(source_name)
    target = normalize_name(wikidata_name)

    if not source or not target:
        return 0

    if source == target:
        return 50

    if normalize_compact(source) == normalize_compact(target):
        return 45

    source_parts = set(source.split())
    target_parts = set(target.split())

    if source_parts and target_parts:

        intersection = source_parts.intersection(target_parts)

        if intersection:

            ratio = len(intersection) / max(
                len(source_parts),
                len(target_parts)
            )

            if ratio >= 0.75:
                return 35

            if ratio >= 0.5:
                return 25

    if source in target or target in source:
        return 15

    return 0


# ============================================================
# CANDIDATE EVALUATION
# ============================================================

def evaluate_candidate(player, search_result):

    qid = search_result.get("id")

    if not qid:
        return None

    entity = get_entity(qid)

    if not entity:
        return None

    source_name = (
        player.get("displayName")
        or player.get("name")
        or ""
    )

    label = (
        entity.get("labels", {})
        .get("en", {})
        .get("value", "")
    )

    description = get_entity_description(entity)

    source_date = player.get("dateOfBirth")

    source_precision = player.get(
        "dateOfBirthPrecision"
    )

    source_year = get_year_from_date(source_date)

    wikidata_dob, wikidata_precision = extract_wikidata_dob(
        entity
    )

    wikidata_year = get_year_from_date(
        wikidata_dob
    )

    score = 0

    score += name_similarity(
        source_name,
        label
    )

    score += football_relevance(
        description
    )

    # --------------------------------------------------------
    # DATE SCORING
    # --------------------------------------------------------

    year_match = False
    year_conflict = False

    if source_year and wikidata_year:

        if source_year == wikidata_year:

            year_match = True

            # Strong evidence
            score += 20

        else:

            year_conflict = True

            # IMPORTANT:
            # Do NOT reward a conflicting birth year.
            #
            # We still keep the candidate so it can be
            # recorded in dob-conflicts-test.json.
            score -= 40

    # Full-date source
    if source_precision == "day":

        if (
            source_date
            and wikidata_dob
            and source_date == wikidata_dob
        ):
            score += 40

    return {
        "qid": qid,
        "entity": entity,
        "label": label,
        "description": description,
        "wikidataDob": wikidata_dob,
        "wikidataPrecision": wikidata_precision,
        "wikidataYear": wikidata_year,
        "sourceYear": source_year,
        "yearMatch": year_match,
        "yearConflict": year_conflict,
        "score": score
    }


# ============================================================
# FIND BEST CANDIDATE
# ============================================================

def find_best_candidate(player):

    source_name = (
        player.get("displayName")
        or player.get("name")
        or ""
    )

    source_date = player.get("dateOfBirth")

    source_precision = player.get(
        "dateOfBirthPrecision"
    )

    source_year = get_year_from_date(source_date)

    search_results = wikidata_search(
        source_name,
        limit=10
    )

    candidates = []

    for result in search_results:

        candidate = evaluate_candidate(
            player,
            result
        )

        if candidate:
            candidates.append(candidate)

        time.sleep(REQUEST_DELAY)

    if not candidates:
        return None, None

    # --------------------------------------------------------
    # SAFETY RULE FOR YEAR-ONLY SOURCES
    # --------------------------------------------------------
    #
    # When source precision is YEAR:
    #
    # 1. Prefer candidates whose Wikidata year matches.
    # 2. Never automatically upgrade using a conflicting year.
    #
    # This is the key V2.2 fix.
    # --------------------------------------------------------

    if source_precision == "year" and source_year:

        matching = [
            c for c in candidates
            if c["yearMatch"]
        ]

        if matching:

            matching.sort(
                key=lambda x: x["score"],
                reverse=True
            )

            return matching[0], candidates

        # No matching-year candidate.
        #
        # Return the strongest candidate for audit purposes,
        # but it MUST NOT be used to change the DOB.
        candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return None, candidates

    # --------------------------------------------------------
    # Full-date / other precision
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return candidates[0], candidates


# ============================================================
# MATCH METHOD
# ============================================================

def determine_match_method(
    source_precision,
    wikidata_precision,
    source_date,
    wikidata_date
):

    if not wikidata_date:
        return None

    source_year = get_year_from_date(source_date)
    wikidata_year = get_year_from_date(wikidata_date)

    if (
        source_precision == "year"
        and wikidata_precision == "day"
        and source_year
        and wikidata_year
        and source_year == wikidata_year
    ):
        return "year-to-full-dob"

    if (
        source_precision == "year"
        and wikidata_precision == "month"
        and source_year
        and wikidata_year
        and source_year == wikidata_year
    ):
        return "year-to-month"

    if (
        source_precision == "year"
        and wikidata_precision == "year"
        and source_year
        and wikidata_year
        and source_year == wikidata_year
    ):
        return "year-only"

    if (
        source_precision == "month"
        and wikidata_precision == "day"
        and source_year
        and wikidata_year
        and source_year == wikidata_year
    ):
        return "month-to-full-dob"

    if (
        source_precision == "day"
        and source_date == wikidata_date
    ):
        return "full-date-match"

    return None


# ============================================================
# MAIN ENRICHMENT
# ============================================================

def main():

    print("=" * 60)
    print("Fyucha Player Database - Wikidata V2.2")
    print("=" * 60)
    print()

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

    print(
        f"Loaded {len(players):,} total players."
    )

    test_players = players[:TEST_LIMIT]

    print(
        f"Testing first {len(test_players)} players."
    )

    print()

    enriched_players = []

    matches = []

    corrections = []

    conflicts = []

    year_only_records = []

    matched_count = 0
    full_dob_count = 0
    month_count = 0
    year_count = 0
    correction_count = 0
    conflict_count = 0
    error_count = 0
    deceased_count = 0

    # ========================================================
    # PROCESS PLAYERS
    # ========================================================

    for index, player in enumerate(test_players, start=1):

        original = dict(player)

        name = (
            player.get("displayName")
            or player.get("name")
            or "Unknown"
        )

        source_date = player.get(
            "dateOfBirth"
        )

        source_precision = player.get(
            "dateOfBirthPrecision"
        )

        if not source_precision:

            _, source_precision = parse_source_date(
                source_date
            )

        print(
            f"[{index}/{len(test_players)}] {name}"
        )

        try:

            best, all_candidates = find_best_candidate(
                player
            )

            # ------------------------------------------------
            # NO SAFE MATCH
            # ------------------------------------------------

            if not best:

                # Check whether there was a likely candidate
                # with a conflicting birth year.
                if all_candidates:

                    strongest = all_candidates[0]

                    if (
                        source_precision == "year"
                        and strongest.get("yearConflict")
                    ):

                        conflict_count += 1

                        conflicts.append({
                            "id": player.get("id"),
                            "name": name,
                            "sourceDateOfBirth": source_date,
                            "sourceDatePrecision": source_precision,
                            "sourceBirthYear": strongest.get(
                                "sourceYear"
                            ),
                            "candidateDateOfBirth": strongest.get(
                                "wikidataDob"
                            ),
                            "candidateBirthYear": strongest.get(
                                "wikidataYear"
                            ),
                            "candidateName": strongest.get(
                                "label"
                            ),
                            "candidateDescription": strongest.get(
                                "description"
                            ),
                            "wikidataId": strongest.get(
                                "qid"
                            ),
                            "matchScore": strongest.get(
                                "score"
                            ),
                            "conflictType": "birth-year-mismatch",
                            "action": "kept-source-dob"
                        })

                if source_precision == "year":

                    year_count += 1

                    year_only_records.append({
                        "id": player.get("id"),
                        "name": name,
                        "dateOfBirth": source_date,
                        "dateOfBirthPrecision": "year",
                        "matchMethod": None,
                        "wikidataId": None
                    })

                enriched_players.append(
                    original
                )

                continue

            # ------------------------------------------------
            # SAFE MATCH FOUND
            # ------------------------------------------------

            matched_count += 1

            qid = best["qid"]

            wikidata_dob = best["wikidataDob"]

            wikidata_precision = best[
                "wikidataPrecision"
            ]

            method = determine_match_method(
                source_precision,
                wikidata_precision,
                source_date,
                wikidata_dob
            )

            if not method:

                # Safety fallback:
                # Do not alter source DOB if the match method
                # cannot be safely established.

                enriched_players.append(
                    original
                )

                continue

            # ------------------------------------------------
            # COPY PLAYER
            # ------------------------------------------------

            enriched = dict(player)

            # Wikidata ID
            enriched["wikidataId"] = qid

            # Wikidata metadata
            enriched["wikidataDateOfBirth"] = wikidata_dob

            enriched["wikidataDateOfBirthPrecision"] = (
                wikidata_precision
            )

            enriched["wikidataMatchScore"] = best[
                "score"
            ]

            enriched["wikidataMatchMethod"] = method

            # ------------------------------------------------
            # APPLY DOB UPGRADE
            # ------------------------------------------------

            old_date = enriched.get(
                "dateOfBirth"
            )

            old_precision = enriched.get(
                "dateOfBirthPrecision"
            )

            if (
                source_precision == "year"
                and wikidata_precision == "day"
            ):

                source_year = get_year_from_date(
                    source_date
                )

                wikidata_year = get_year_from_date(
                    wikidata_dob
                )

                # V2.2 FINAL SAFETY CHECK
                if source_year != wikidata_year:

                    conflicts.append({
                        "id": player.get("id"),
                        "name": name,
                        "sourceDateOfBirth": source_date,
                        "sourceDatePrecision": source_precision,
                        "sourceBirthYear": source_year,
                        "candidateDateOfBirth": wikidata_dob,
                        "candidateBirthYear": wikidata_year,
                        "candidateName": best.get(
                            "label"
                        ),
                        "candidateDescription": best.get(
                            "description"
                        ),
                        "wikidataId": qid,
                        "matchScore": best.get(
                            "score"
                        ),
                        "conflictType": "birth-year-mismatch",
                        "action": "kept-source-dob"
                    })

                    enriched = dict(player)

                    enriched["wikidataId"] = qid

                    enriched["wikidataDateOfBirth"] = (
                        wikidata_dob
                    )

                    enriched["wikidataDateOfBirthPrecision"] = (
                        wikidata_precision
                    )

                    enriched["wikidataMatchScore"] = best[
                        "score"
                    ]

                    enriched["wikidataMatchMethod"] = (
                        "year-conflict"
                    )

                    enriched_players.append(
                        enriched
                    )

                    continue

                # SAFE UPGRADE
                enriched["dateOfBirth"] = (
                    wikidata_dob
                )

                enriched["dateOfBirthPrecision"] = (
                    "day"
                )

                correction_count += 1

                corrections.append({
                    "id": player.get("id"),
                    "name": name,
                    "sourceDateOfBirth": old_date,
                    "sourceDatePrecision": old_precision,
                    "correctedDateOfBirth": wikidata_dob,
                    "correctedDatePrecision": "day",
                    "matchMethod": method,
                    "matchScore": best["score"],
                    "wikidataId": qid
                })

                full_dob_count += 1

            elif (
                source_precision == "year"
                and wikidata_precision == "month"
            ):

                source_year = get_year_from_date(
                    source_date
                )

                wikidata_year = get_year_from_date(
                    wikidata_dob
                )

                if source_year == wikidata_year:

                    enriched["dateOfBirth"] = (
                        wikidata_dob
                    )

                    enriched["dateOfBirthPrecision"] = (
                        "month"
                    )

                    month_count += 1

            elif (
                source_precision == "year"
                and wikidata_precision == "year"
            ):

                year_count += 1

                year_only_records.append({
                    "id": player.get("id"),
                    "name": name,
                    "dateOfBirth": source_date,
                    "dateOfBirthPrecision": "year",
                    "matchMethod": method,
                    "wikidataId": qid
                })

            # ------------------------------------------------
            # DECEASED
            # ------------------------------------------------

            death_value = get_claim_value(
                best["entity"],
                "P570"
            )

            if death_value:

                deceased_count += 1

                enriched["wikidataDateOfDeath"] = (
                    death_value.get("time")
                    if isinstance(
                        death_value,
                        dict
                    )
                    else death_value
                )

            # ------------------------------------------------
            # MATCH AUDIT
            # ------------------------------------------------

            matches.append({
                "id": player.get("id"),
                "name": name,
                "wikidataId": qid,
                "wikidataName": best["label"],
                "wikidataDescription": best[
                    "description"
                ],
                "sourceDateOfBirth": source_date,
                "sourceDatePrecision": source_precision,
                "wikidataDateOfBirth": wikidata_dob,
                "wikidataDateOfBirthPrecision": (
                    wikidata_precision
                ),
                "matchMethod": method,
                "matchScore": best["score"],
                "yearMatch": best["yearMatch"]
            })

            enriched_players.append(
                enriched
            )

        except Exception as exc:

            error_count += 1

            print(
                f"  ERROR: {exc}"
            )

            enriched_players.append(
                original
            )

    # ========================================================
    # SAVE OUTPUTS
    # ========================================================

    ENRICHED_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        ENRICHED_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            enriched_players,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        MATCHES_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            matches,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CORRECTIONS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            corrections,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CONFLICTS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            conflicts,
            f,
            ensure_ascii=False,
            indent=2
        )

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

    print()
    print("=" * 60)
    print("V2.2 TEST COMPLETE")
    print("=" * 60)

    print(
        f"Players tested:        {len(test_players)}"
    )

    print(
        f"Matched:               {matched_count}"
    )

    print(
        f"Full DOB records:      {full_dob_count}"
    )

    print(
        f"Month precision:       {month_count}"
    )

    print(
        f"Year precision:        {year_count}"
    )

    print(
        f"DOB corrections:       {correction_count}"
    )

    print(
        f"DOB conflicts:         {conflict_count}"
    )

    print(
        f"Deceased records:      {deceased_count}"
    )

    print(
        f"Errors:                {error_count}"
    )

    print()
    print("Output files:")
    print(
        f"  {ENRICHED_FILE}"
    )
    print(
        f"  {MATCHES_FILE}"
    )
    print(
        f"  {CORRECTIONS_FILE}"
    )
    print(
        f"  {CONFLICTS_FILE}"
    )
    print(
        f"  {YEAR_ONLY_FILE}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
